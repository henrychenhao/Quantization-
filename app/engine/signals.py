"""实时监控引擎：对开启量化的自选标的逐一跑绑定策略，生成信号。

五道前置校验闸门（对齐参考系统行为）：
1. 价格校验    —— 现价有效（非空非零）
2. 涨跌停过滤  —— 涨停买不进、跌停卖不出，信号无意义直接跳过
3. 资金/持仓校验 —— 买入信号需可用现金覆盖（预计金额+佣金），不足收缩股数，
                  不足一手则跳过；卖出信号需有持仓，建议卖出现有持仓股数
4. 去重校验    —— 同一标的×同一策略当日仅一条未决信号
5. 组合级风控  —— 买入信号校验总仓位/单标的/现金预留上限（config.yaml risk 段，0=不限）

产出：信号入库 + 企微推送 + 推送日志。此模块不自动下单。
"""
import json

import pandas as pd

from app import db
from app.account import core as account
from app.backtest.engine import Fees
from app.datasource.akshare_source import AkShareSource
from app.datasource.base import is_etf
from app.datasource.kline_store import get_or_fetch, load_local
from app.notify import wecom
from app.strategy.registry import STRATEGIES


def _portfolio_market_value(kline_dir: str) -> float:
    """组合持仓总市值：各持仓股数 × 本地 K 线最新收盘（无网络开销）。"""
    mv = 0.0
    for code, p in account.positions().items():
        df = load_local(code, kline_dir)
        if df is not None and not df.empty:
            mv += p["shares"] * float(df["close"].iloc[-1])
    return mv


def _combo_risk_check(code: str, side: str, price: float, shares: int, fee: float,
                      risk: dict, kline_dir: str, cash: float) -> str | None:
    """闸门5：组合级风控（仅买入信号）。返回拦截原因，None 表示放行。

    risk 段（config.yaml）：
      max_position_ratio  买入后全部持仓市值 / 总资产 上限（0=不限）
      max_single_ratio    买入后该标的市值 / 总资产 上限（0=不限）
      min_cash_ratio      买入后现金 ≥ 初始资金 × 该比例（0=不限）
    均为 0 或未配置时不限制，保持原有行为。
    """
    if side != "buy":
        return None
    max_pos = float(risk.get("max_position_ratio") or 0)
    max_single = float(risk.get("max_single_ratio") or 0)
    min_cash = float(risk.get("min_cash_ratio") or 0)
    if not (max_pos or max_single or min_cash):
        return None

    mv = _portfolio_market_value(kline_dir)
    total = cash + mv
    if total <= 0:
        return None
    buy_mv = price * shares
    this_mv = buy_mv
    pos = account.position_of(code)
    if pos:
        this_mv += pos["shares"] * price
    new_mv = mv + this_mv
    new_cash = cash - buy_mv - fee
    if max_pos and new_mv / total > max_pos:
        return f"组合级风控：买入后仓位 {new_mv / total:.0%} 超上限 {max_pos:.0%}"
    if max_single and this_mv / total > max_single:
        return f"组合级风控：该标的占比 {this_mv / total:.0%} 超单标的上限 {max_single:.0%}"
    if min_cash and new_cash < min_cash * db.get_initial_cash():
        return f"组合级风控：买入后现金 ¥{new_cash:,.0f} 低于预留 ¥{min_cash * db.get_initial_cash():,.0f}"
    return None


def _estimate_buy_shares(cash: float, price: float, fees: Fees) -> int:
    """按可用现金估算整手买入股数（预留滑点+佣金）。"""
    if price <= 0:
        return 0
    exec_price = price * (1 + fees.slippage)
    shares = int((cash / (exec_price * (1 + fees.commission))) // 100 * 100)
    return max(shares, 0)


def _limit_pct(code: str, name: str = "") -> float | None:
    """按板块返回涨跌停幅度：主板 10%、创业板/科创板 20%、北交所 30%、ST 5%。

    ETF 等场内基金返回 None 不强制过滤（跨境 ETF 涨跌幅制度不同，避免误判）。
    """
    if is_etf(code):
        return None
    if code.startswith(("688", "300", "301")):
        return 0.20
    if code.startswith(("8", "4", "92")):
        return 0.30
    if "ST" in (name or "").upper():
        return 0.05
    return 0.10


def run_signals(cfg: dict, dry_run: bool = False, spot_mode: bool = False) -> dict:
    """扫描全部开启的标的 → 校验 → 生成信号 → 推送。

    dry_run=True：只返回将要生成的信号，不落库不推送（页面预览用）。
    spot_mode=True：盘中模式——用新浪实时价追加"今日"一根 K 线再判断，
    信号反映盘中行情而非昨日收盘（历史 K 线不被污染）。
    返回 {generated: [...], skipped: [...], codes: n}
    """
    items = [w for w in db.list_watchlist() if w["quant_enabled"]]
    fees = Fees(**cfg.get("fees", {}))
    kline_dir = cfg["data"]["kline_dir"]
    source = AkShareSource()
    wecom.init(cfg.get("push", {}))          # 信号推送前确保 webhook 就绪（幂等）

    generated, skipped = [], []
    cash = account.cash()

    for w in items:
        code, name = w["code"], w["name"] or w["code"]
        try:
            strategies = json.loads(w["strategies"] or "[]")
        except json.JSONDecodeError:
            strategies = []
        if not strategies:
            skipped.append({"code": code, "reason": "未绑定策略"})
            continue

        try:
            df = get_or_fetch(code, source, kline_dir)
        except Exception as e:
            skipped.append({"code": code, "reason": f"行情获取失败：{e}"})
            continue
        if df is None or df.empty:
            skipped.append({"code": code, "reason": "无行情数据"})
            continue

        # 盘中模式：实时价追加一根"今日"K 线（指标不含未来数据，历史不被污染）
        spot_info = None
        if spot_mode:
            info = source.get_spots([code]).get(code)
            if not info or not (info.get("price", 0) > 0):
                skipped.append({"code": code, "reason": "实时价获取失败/停牌"})
                continue
            spot_info = info                    # 未复权原始值（涨跌停判定用）
            spot = info["price"]
            today = pd.Timestamp.now().normalize()
            if df.index[-1].normalize() < today:
                # 复权口径统一：历史 K 线为前复权，实时价为未复权。
                # 近期除权后两者会偏差，用「qfq 昨收 ÷ 未复权昨收」作因子对齐；
                # 无除权时因子 = 1，无副作用。
                raw_prev = info.get("prev_close") or 0.0
                qfq_prev = float(df["close"].iloc[-1])
                if raw_prev > 0 and qfq_prev > 0:
                    spot = spot * (qfq_prev / raw_prev)
                last = df.iloc[-1]
                row = {"open": float(last["open"]), "high": max(float(last["close"]), spot),
                       "low": min(float(last["close"]), spot), "close": spot,
                       "volume": 0.0, "amount": 0.0}
                if "amount" not in df.columns:
                    row.pop("amount")
                df = pd.concat([df, pd.DataFrame([row], index=[today])])

        price = float(df["close"].iloc[-1])
        # 闸门1：价格校验
        if not (price > 0) or pd.isna(price):
            skipped.append({"code": code, "reason": "价格异常/停牌"})
            continue

        # 昨收（涨跌停判定基准）：盘中用新浪未复权昨收；日 K 模式用前一交易日收盘
        prev_close = 0.0
        if spot_info and spot_info.get("prev_close"):
            prev_close = float(spot_info["prev_close"])
        elif len(df) >= 2:
            prev_close = float(df["close"].iloc[-2])

        pos = account.position_of(code)
        ctx = {
            "cash": cash,
            "shares": float(pos["shares"]) if pos else 0.0,
            "avg_cost": float(pos["avg_cost"]) if pos else 0.0,
            "last_price": db.last_trade_price(code),   # 网格锚点
        }
        for sc in strategies:
            if sc not in STRATEGIES:
                continue
            # 闸门4：去重校验
            if not dry_run and db.duplicate_today(code, sc):
                skipped.append({"code": code, "reason": f"{sc} 当日已推送过（含已忽略），不重复"})
                continue
            strategy = STRATEGIES[sc]()
            # 网格等多档策略：由持仓量估算已持有档数（每档股数按初始资金占比估算）
            if getattr(strategy, "multi_lot", False) and pos:
                lot_shares = max(100, int((db.get_initial_cash() * strategy.lot_ratio) / price // 100 * 100))
                ctx["grid_lots"] = max(1, round(pos["shares"] / lot_shares)) if lot_shares else 1
            else:
                ctx["grid_lots"] = 0
            sdf = strategy.compute(df)
            i = len(sdf) - 1
            side = strategy.signal(sdf, i, ctx) if i >= strategy.warmup() else None
            if side is None:
                continue

            reason = strategy.explain(sdf, i, side)
            # 闸门2：涨跌停/停牌过滤（涨停买不进、跌停卖不出，信号无意义）
            limit_pct = _limit_pct(code, name)
            if side == "buy" and limit_pct and prev_close > 0 \
                    and price >= prev_close * (1 + limit_pct) - 1e-4:
                skipped.append({"code": code, "reason": f"{sc} 现价涨停，无法买入（{reason}）"})
                continue
            if side == "sell" and limit_pct and prev_close > 0 \
                    and price <= prev_close * (1 - limit_pct) + 1e-4:
                skipped.append({"code": code, "reason": f"{sc} 现价跌停，无法卖出（{reason}）"})
                continue

            # 闸门3：资金与持仓（分档策略按 lot_ratio 占比估算）
            if side == "buy":
                budget = cash * getattr(strategy, "lot_ratio", 1.0)
                shares = _estimate_buy_shares(budget, price, fees)
                if shares < 100:
                    skipped.append({"code": code, "reason": f"{sc} 资金不足，跳过（{reason}）"})
                    continue
                # 闸门5：组合级风控（总仓位/单标的/现金预留）
                risk_fee = max(price * shares * fees.commission, fees.min_commission)
                risk_reason = _combo_risk_check(code, side, price, shares, risk_fee,
                                                cfg.get("risk", {}), kline_dir, cash)
                if risk_reason:
                    skipped.append({"code": code, "reason": f"{sc} {risk_reason}"})
                    continue
            else:
                if pos is None or pos["shares"] <= 0:
                    skipped.append({"code": code, "reason": f"{sc} 无持仓，跳过（{reason}）"})
                    continue
                shares = pos["shares"]

            amount = round(price * shares, 2)
            if dry_run:
                generated.append({"code": code, "side": side, "strategy": sc,
                                  "reason": reason, "price": price, "shares": shares})
                continue

            sid = db.add_signal(code, name, side, sc, reason, price, shares)
            # 推送（webhook 未配置时自动降级为 mock + push_log）
            content = wecom.signal_msg(code, name, side, strategy.name, reason,
                                       price, shares, amount, cash=cash if side == "buy" else None)
            status = wecom.push(f"交易信号 {code}", content)
            generated.append({"id": sid, "code": code, "side": side, "strategy": sc,
                              "reason": reason, "price": price, "shares": shares,
                              "push": status})

    return {"generated": generated, "skipped": skipped,
            "watch": len(items), "cash": account.cash()}