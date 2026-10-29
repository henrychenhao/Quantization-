"""实时监控引擎：对开启量化的自选标的逐一跑绑定策略，生成信号。

四道前置校验闸门（对齐参考系统行为）：
1. 价格校验    —— 现价有效（非空非零）
2. 资金校验    —— 买入信号需可用现金覆盖（预计金额+佣金），不足收缩股数，不足一手则跳过
3. 持仓校验    —— 卖出信号需有持仓，建议卖出现有持仓股数
4. 去重校验    —— 同一标的×同一策略当日仅一条未决信号

产出：信号入库 + 企微推送 + 推送日志。此模块不自动下单。
"""
import json

import pandas as pd

from app import db
from app.account import core as account
from app.backtest.engine import Fees
from app.datasource.akshare_source import AkShareSource
from app.datasource.kline_store import get_or_fetch
from app.notify import wecom
from app.strategy.registry import STRATEGIES


def _estimate_buy_shares(cash: float, price: float, fees: Fees) -> int:
    """按可用现金估算整手买入股数（预留滑点+佣金）。"""
    if price <= 0:
        return 0
    exec_price = price * (1 + fees.slippage)
    shares = int((cash / (exec_price * (1 + fees.commission))) // 100 * 100)
    return max(shares, 0)


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

        # 盘中模式：实时价追加一根"今日"K 线（仅 close 有意义，指标不含未来数据）
        if spot_mode:
            spot = source.get_spot(code)
            if not (spot and spot > 0):
                skipped.append({"code": code, "reason": "实时价获取失败/停牌"})
                continue
            today = pd.Timestamp.now().normalize()
            if df.index[-1].normalize() < today:
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
                skipped.append({"code": code, "reason": f"{sc} 当日已有信号"})
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
            # 闸门2/3：资金与持仓（分档策略按 lot_ratio 占比估算）
            if side == "buy":
                budget = cash * getattr(strategy, "lot_ratio", 1.0)
                shares = _estimate_buy_shares(budget, price, fees)
                if shares < 100:
                    skipped.append({"code": code, "reason": f"{sc} 资金不足，跳过（{reason}）"})
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