"""AI 分析师：把本地真实数据组装成结构化上下文，喂给大模型做有据分析。

四类能力对应四类上下文：
- 个股诊断：行情摘要 + 五指标读数 + 六策略当前触发状态 + 该标的持仓
- 持仓复盘：账户概况 + 每只持仓的成本/浮盈 + 近期信号质量
- 信号解读：最近待处理信号 + 近期历史信号
- 策略问答：内置六策略原理说明（不注入行情）

设计原则：数据事实全部由本地计算生成，模型只负责解读，禁止编造数字。
"""
import json

import pandas as pd

from app import db
from app.account import core as account
from app.datasource.akshare_source import AkShareSource
from app.datasource.base import is_etf
from app.datasource.kline_store import get_or_fetch
from app.engine.outcome import strategy_stats
from app.indicators import boll, donchian_high, donchian_low, ma, macd, rsi
from app.strategy.registry import STRATEGIES

_STRATEGY_KNOWLEDGE = {
    "double_ma": "双均线交叉：MA5 上穿 MA20（金叉）买入，下穿（死叉）卖出。趋势跟随型，震荡市易被反复打脸。",
    "macd": "MACD 金叉死叉：DIF 上穿 DEA 买入、下穿卖出。对趋势启动敏感，滞后于均线但噪音更小。",
    "boll_mr": "布林带均值回归：收盘跌破下轨（超卖）买入，收盘回到中轨上方卖出。适合震荡市，单边下跌会连续接飞刀。",
    "donchian": "唐奇安通道突破：收盘突破前 20 日最高买入，跌破前 10 日最低卖出。经典的趋势突破（海龟）逻辑。",
    "rsi": "RSI 超卖回归：RSI(14) 下穿 30 买入、上穿 70 卖出。均值回归型，强势单边行情中会过早离场。",
    "grid": "网格交易（多档）：锚点价以下每跌 3% 买入一档（最多 5 档，每档 20% 资金），涨到持仓均价 +3% 止盈清仓。适合低波动区间震荡，单边下跌会满仓套牢。",
}

_DISCLAIMER = "（本分析由 AI 基于系统内数据生成，仅供参考，不构成投资建议。）"


def _fmt(v, digits=3):
    try:
        if pd.isna(v):
            return "—"
        return f"{float(v):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _pct(v, digits=2):
    try:
        if pd.isna(v):
            return "—"
        return f"{float(v) * 100:+.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def _account_ctx_for(code: str) -> dict:
    """构造与信号引擎一致的账户上下文（现金/持仓/网格锚点与档位）。"""
    pos = account.position_of(code)
    return {
        "cash": account.cash(),
        "shares": float(pos["shares"]) if pos else 0.0,
        "avg_cost": float(pos["avg_cost"]) if pos else 0.0,
        "last_price": db.last_trade_price(code),
        "grid_lots": 0,
    }


def _volume_ctx(df) -> str:
    """量能上下文（纯本地计算，零网络）：量比 / 均量对比 / 连续量能 / 量价配合。

    只用相对值（比值、天数、状态），不展示绝对手数，避免数据源单位差异。
    """
    vol, close = df["volume"], df["close"]
    if len(vol) < 25 or float(vol.tail(20).mean()) <= 0:
        return ""
    ma20 = float(vol.tail(20).mean())
    # 量比：今日量 / 前 5 日均量（不含今日）
    ma5_prev = float(vol.iloc[-6:-1].mean())
    ratio = float(vol.iloc[-1]) / ma5_prev if ma5_prev > 0 else None
    # 近 5 日均量 vs 20 日均量
    amp = float(vol.tail(5).mean()) / ma20
    amp_txt = "放量" if amp > 1.15 else ("缩量" if amp < 0.85 else "持平")
    # 连续高于 20 日均量的天数（从最新往回）
    n_hot = 0
    for v in reversed(vol.tolist()):
        if v > ma20:
            n_hot += 1
        else:
            break
    # 近 5 日量价配合：涨放量/跌缩量 = 健康；涨缩量/跌放量 = 背离
    up_hot = up_cold = dn_hot = dn_cold = 0
    for i in range(len(df) - 5, len(df)):
        chg = float(close.iloc[i]) - float(close.iloc[i - 1])
        hot = float(vol.iloc[i]) > ma20
        if chg >= 0:
            up_hot += hot; up_cold += (not hot)
        else:
            dn_hot += hot; dn_cold += (not hot)
    if (up_hot + dn_cold) >= (up_cold + dn_hot):
        vp_txt = "量价配合健康（涨放量、跌缩量）"
    else:
        vp_txt = "量价背离（涨缩量或跌放量，动能存疑）"
    parts = []
    if ratio is not None:
        parts.append(f"今日量比 {ratio:.2f}")
    parts.append(f"近5日均量为20日均量的 {amp:.0%}（{amp_txt}）")
    parts.append(f"连续 {n_hot} 日高于20日均量")
    parts.append(f"近5日：涨放量{up_hot}/涨缩量{up_cold}/跌放量{dn_hot}/跌缩量{dn_cold}，{vp_txt}")
    return "【量能】" + "；".join(parts)


def stock_context(code: str, cfg: dict) -> str:
    """个股诊断上下文：行情摘要 + 指标读数 + 六策略触发状态 + 持仓。"""
    source = AkShareSource()
    df = get_or_fetch(code, source, cfg["data"]["kline_dir"])
    if df is None or df.empty or len(df) < 30:
        return f"【{code}】本地行情数据不足（不足 30 根 K 线），无法生成技术面上下文。"

    w = next((x for x in db.list_watchlist() if x["code"] == code), None)
    name = (w["name"] if w else "") or code
    close = df["close"]
    last = float(close.iloc[-1])
    date = df.index[-1].strftime("%Y-%m-%d")

    def window_pct(n):
        if len(close) <= n:
            return None
        return last / float(close.iloc[-n - 1]) - 1

    lines = [
        f"【标的】{code} {name}（{'ETF，T+0 可当日买卖、免印花税' if is_etf(code) else 'A股个股，T+1'}）",
        f"【行情】截至 {date} 收盘 {last:.3f}："
        f"近5日 {_pct(window_pct(5))}，近20日 {_pct(window_pct(20))}，"
        f"近60日 {_pct(window_pct(60))}；"
        f"近60日区间 [{float(close.tail(60).min()):.3f}, {float(close.tail(60).max()):.3f}]",
        f"【均线】MA5={_fmt(ma(close, 5).iloc[-1])} MA10={_fmt(ma(close, 10).iloc[-1])} "
        f"MA20={_fmt(ma(close, 20).iloc[-1])} MA60={_fmt(ma(close, 60).iloc[-1])}",
    ]
    dif, dea, _ = macd(close)
    mid, upper, lower = boll(close)
    r14 = rsi(close)
    dhi = donchian_high(df["high"].shift(1), 20)
    dlo = donchian_low(df["low"].shift(1), 10)
    lines.append(
        f"【指标】MACD：DIF={_fmt(dif.iloc[-1])} DEA={_fmt(dea.iloc[-1])}"
        f"（{'DIF 在 DEA 上方' if dif.iloc[-1] >= dea.iloc[-1] else 'DIF 在 DEA 下方'}）；"
        f"RSI14={_fmt(r14.iloc[-1], 1)}；"
        f"布林(20,2)：中轨 {_fmt(mid.iloc[-1])} 上轨 {_fmt(upper.iloc[-1])} 下轨 {_fmt(lower.iloc[-1])}，"
        f"现价位于{'中轨上方' if last >= mid.iloc[-1] else '中轨下方'}")
    lines.append(
        f"【通道】20日唐奇安上轨 {_fmt(dhi.iloc[-1])}（{'已突破' if last > dhi.iloc[-1] else '未突破'}）；"
        f"10日下轨 {_fmt(dlo.iloc[-1])}（{'已跌破' if last < dlo.iloc[-1] else '未跌破'}）")
    vol_line = _volume_ctx(df)
    if vol_line:
        lines.append(vol_line)

    # 腾讯快照：五档盘口压力 + 个股基本面（ETF 无 PE/PB 概念，跳过基本面）
    try:
        from app.datasource.tencent import get_quotes
        snap = get_quotes([code]).get(code)
    except Exception:
        snap = None
    if snap and snap["price"] > 0:
        if snap["bid_vol"] + snap["ask_vol"] > 0:
            pressure = snap["bid_vol"] / snap["ask_vol"] if snap["ask_vol"] > 0 else 99.0
            lines.append(
                f"【盘口】五档挂单：买盘 {snap['bid_vol']:,.0f} 手 vs 卖盘 {snap['ask_vol']:,.0f} 手"
                f"（买/卖挂单比 {pressure:.2f}；盘中快照，收盘后挂单可能残留）")
        if not is_etf(code) and (snap["pe"] > 0 or snap["pb"] > 0):
            lines.append(
                f"【基本面】PE(TTM) {snap['pe']:.1f}，PB {snap['pb']:.2f}，"
                f"总市值 {snap['mktcap']:,.0f} 亿，换手率 {snap['turnover']:.2f}%，量比 {snap['vol_ratio']:.2f}")

    # 板块联动：自选页配置了行业才有（个股 vs 行业指数强弱）
    industry = ((w["industry"] if w else "") or "").strip()
    if industry and not is_etf(code):
        try:
            from app.datasource.sector import industry_perf
            perf = industry_perf(industry)
        except Exception:
            perf = None
        if perf:
            ind5, ind20 = perf.get("ret_5d"), perf.get("ret_20d")
            stock5, stock20 = window_pct(5), window_pct(20)
            rel = ""
            if ind5 is not None and stock5 is not None:
                rel = "，强于行业" if stock5 > ind5 else "，弱于行业"
            lines.append(
                f"【板块】所属行业「{industry}」：行业指数近5日 {_pct(ind5)}、近20日 {_pct(ind20)}；"
                f"个股近5日 {_pct(stock5)}{rel}")

    # 六策略当前触发状态（与信号引擎同构）
    ctx = _account_ctx_for(code)
    status = []
    for sc, cls in STRATEGIES.items():
        s = cls()
        if sc == "grid" and ctx["shares"] > 0:
            lot_est = max(100, int((db.get_initial_cash() * s.lot_ratio) / last // 100 * 100))
            ctx["grid_lots"] = max(1, round(ctx["shares"] / lot_est)) if lot_est else 1
        sdf = s.compute(df)
        i = len(sdf) - 1
        side = s.signal(sdf, i, ctx) if i >= s.warmup() else None
        status.append(f"{s.name}：{'触发' + ('买入' if side == 'buy' else '卖出') if side else '无信号'}")
    lines.append("【策略状态】" + "；".join(status))
    pos = account.position_of(code)
    lines.append(f"【持仓】{'持有 ' + str(pos['shares']) + ' 股，均价 ' + _fmt(pos['avg_cost'], 4) if pos else '空仓'}"
                 f"（账户可用现金 ¥{account.cash():,.0f}）")
    return "\n".join(lines)


def portfolio_context() -> str:
    """持仓复盘上下文：账户概况 + 持仓明细 + 近期信号。"""
    pos = account.positions()
    init_cash = db.get_initial_cash()
    cash = account.cash()
    lines = [
        f"【账户】初始资金 ¥{init_cash:,.0f}，当前现金 ¥{cash:,.0f}",
        f"【持仓】{len(pos)} 只：" if pos else "【持仓】当前空仓",
    ]
    for code, p in pos.items():
        name = next((w["name"] for w in db.list_watchlist() if w["code"] == code), "") or code
        # 最新价与浮盈：优先本地 K 线收盘（与账户页同口径）
        try:
            from app.datasource.kline_store import load_local
            df = load_local(code, "data/kline")
            last = float(df["close"].iloc[-1]) if df is not None and not df.empty else 0.0
        except Exception:
            last = 0.0
        pnl = (last / p["avg_cost"] - 1) if (last and p["avg_cost"]) else 0.0
        lines.append(f"- {code} {name}：{p['shares']} 股，成本均价 {_fmt(p['avg_cost'], 4)}，"
                     f"最新价 {_fmt(last)}，浮动盈亏 {_pct(pnl)}")
    sigs = db.list_signals()
    pending = [s for s in sigs if s["status"] == "pending"]
    lines.append(f"【信号】待处理 {len(pending)} 条：" if pending else "【信号】暂无待处理信号")
    for s in pending[:5]:
        lines.append(f"- {s['code']} {s['side']} {_fmt(s['ref_price'], 3)} × {s['suggest_shares']} 股"
                     f"（{s['strategy']}，{s['created_at']}）：{(s['reason'] or '')[:60]}")
    recent = [s for s in sigs if s["status"] != "pending"][:8]
    if recent:
        lines.append("【近期已处理信号】")
        for s in recent:
            lines.append(f"- {s['created_at']} {s['code']} {s['side']}（{s['strategy']}）→ {s['status']}：{(s['reason'] or '')[:50]}")
    stats = strategy_stats()
    if stats:
        lines.append("【策略战绩】近20条已结算信号（命中=买入5日涨/卖出5日跌）：")
        for st in stats:
            lines.append(f"- {st['name']}：{st['count']} 条，胜率 {st['win_rate'] * 100:.0f}%，"
                         f"平均 1/5/20 日 {_pct(st['avg_1d'])} / {_pct(st['avg_5d'])} / {_pct(st['avg_20d'])}")
    return "\n".join(lines)


def signal_context() -> str:
    """信号解读上下文：最近待处理信号 + 历史样本。"""
    sigs = db.list_signals()
    pending = [s for s in sigs if s["status"] == "pending"]
    if not pending:
        hist = [f"- {s['created_at']} {s['code']} {s['side']}（{s['strategy']}）→ {s['status']}" for s in sigs[:8]]
        return "【信号】当前没有待处理信号。" + ("\n近期历史：\n" + "\n".join(hist) if hist else "近期无任何信号记录。")
    lines = ["【待处理信号】"]
    for s in pending[:5]:
        name = next((w["name"] for w in db.list_watchlist() if w["code"] == s["code"]), "") or s["code"]
        lines.append(
            f"- {s['created_at']} {s['code']} {name} {'买入' if s['side'] == 'buy' else '卖出'}："
            f"建议 {s['suggest_shares']} 股 @ {_fmt(s['ref_price'], 3)}，"
            f"预计金额 ¥{s['suggest_shares'] * s['ref_price']:,.0f}\n"
            f"  触发：{s['reason']}（策略：{s['strategy']}）")
    lines.append("【策略参数口径】网格多档：每跌3%买一档（最多5档、每档20%资金），涨到均价+3%止盈；"
                 "整手100股；佣金万2.5（最低5元），股票卖出印花税0.05%，ETF免；滑点0.1%。")
    return "\n".join(lines)


def strategy_knowledge() -> str:
    """策略问答上下文：六策略原理（静态知识）。"""
    return "【系统内六策略】\n" + "\n".join(f"- {k}：{v}" for k, v in _STRATEGY_KNOWLEDGE.items())


def system_prompt(scope_code: str | None, cfg: dict) -> str:
    """组装系统提示词：角色 + 规则 + 实时数据上下文。"""
    parts = [
        "你是一位严谨的 A 股量化分析助手，嵌入在一套本地半自动量化交易系统中。",
        "用户通过对话请你分析个股、持仓、信号或策略原理。",
        "规则：1) 只依据下方提供的系统数据进行分析，不要编造任何数字；数据之外的判断要明确说'数据不足'。",
        "2) 遵循 A 股口径：红色代表上涨、绿色代表下跌；股票 T+1、ETF 可 T+0；印花税仅股票卖出收取。",
        "3) 结论分层：先给事实读数，再给解读，最后给风险提示；篇幅克制，重点突出。",
        f"4) 回答末尾不需要重复免责声明（界面已统一展示）。",
    ]
    if scope_code:
        try:
            parts.append(stock_context(scope_code, cfg))
        except Exception as e:
            parts.append(f"【数据】{scope_code} 行情数据获取失败：{e}")
        parts.append("")
    parts.append("【账户概况】" + portfolio_context())
    parts.append("")
    parts.append(strategy_knowledge())
    return "\n".join(parts)
