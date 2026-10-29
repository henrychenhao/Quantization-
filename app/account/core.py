"""账户模块：持仓/现金/资产快照/流水纠错。

设计：交易流水（trade）是唯一事实源——持仓与现金全部由流水推导。
信号"已跟随"即回执记账；历史流水写错时可修改流水，系统按初始资金
+ 全部流水重新推导现金与持仓，保证账本始终自洽。
"""
from collections import defaultdict

import pandas as pd

from app import db


def _derive(rows) -> tuple[dict, float]:
    """由流水行推导 (持仓, 现金)。rows 顺序不影响结果。"""
    agg: dict[str, dict] = defaultdict(lambda: {"shares": 0.0, "cost": 0.0, "fees": 0.0})
    cash = db.get_initial_cash()
    for t in rows:
        a = agg[t["code"]]
        amt = t["price"] * t["shares"]
        if t["side"] == "buy":
            a["shares"] += t["shares"]
            a["cost"] += amt + t["fee"]
            cash -= amt + t["fee"]
        else:
            a["shares"] -= t["shares"]
            a["cost"] -= amt
            a["fees"] += t["fee"]
            cash += amt - t["fee"]
    out = {}
    for code, a in agg.items():
        if a["shares"] > 0:
            out[code] = {"shares": int(a["shares"]), "avg_cost": round(a["cost"] / a["shares"], 4)}
    return out, cash


def positions() -> dict[str, dict]:
    """由成交流水推导当前持仓：{code: {shares, avg_cost}}（含费用摊入成本）。"""
    return _derive(db.list_trades())[0]


def position_of(code: str) -> dict | None:
    return positions().get(code)


def cash() -> float:
    return db.get_cash()


def follow_signal(sid: int, price: float, shares: int, fee: float = 0.0, note: str = "") -> dict:
    """记账：按信号回填成交流水，重算现金与信号状态。返回摘要。"""
    sig = db.get_signal(sid)
    if sig is None:
        raise ValueError("信号不存在")
    if sig["status"] != "pending":
        raise ValueError(f"信号状态为 {sig['status']}，仅 pending 可记账")

    cash_now = db.get_cash()
    amount = price * shares
    if sig["side"] == "buy":
        if amount + fee > cash_now:
            raise ValueError(f"现金不足：需要 ¥{amount + fee:,.0f}，可用 ¥{cash_now:,.0f}")
    else:
        pos = position_of(sig["code"])
        if pos is None or pos["shares"] < shares:
            raise ValueError(f"持仓不足：{sig['code']} 持仓 {pos['shares'] if pos else 0}，卖 {shares}")

    db.add_trade(sid, sig["code"], sig["side"], price, shares, fee, note=note)
    _, new_cash = _derive(db.list_trades())
    db.set_cash(new_cash)
    db.set_signal_status(sid, "followed")
    return {"code": sig["code"], "side": sig["side"], "shares": shares, "price": price,
            "fee": fee, "amount": round(amount, 2)}


def update_trade(tid: int, price: float, shares: int, fee: float, note: str | None = None) -> dict:
    """修改流水（账本纠错）：更新该行后按全部流水重推现金与持仓。

    校验：价格/数量为正、费用非负；修改后现金与持仓不得为负，否则拒绝。
    """
    t = db.get_trade(tid)
    if t is None:
        raise ValueError("流水不存在")
    if price <= 0 or shares <= 0 or fee < 0:
        raise ValueError("价格与数量必须大于 0，费用不能为负")

    rows = [dict(r) for r in db.list_trades()]
    for r in rows:
        if r["id"] == tid:
            r["price"], r["shares"], r["fee"] = price, shares, fee
            if note is not None:
                r["note"] = note
            break

    pos, new_cash = _derive(rows)
    if new_cash < -1e-6:
        raise ValueError(f"修改后现金为负（¥{new_cash:,.2f}），请检查价格与数量")
    # 逐标的净持仓不得为负（_derive 会丢弃负持仓，需单独检查）
    net: dict[str, float] = defaultdict(float)
    for r in rows:
        net[r["code"]] += r["shares"] if r["side"] == "buy" else -r["shares"]
    if any(v < -1e-6 for v in net.values()):
        raise ValueError("修改后持仓为负，请检查数量")

    db.update_trade(tid, price, shares, fee, note)
    db.set_cash(new_cash)
    return {"ok": True, "cash": round(new_cash, 2), "positions": {k: v["shares"] for k, v in pos.items()}}


def snapshot(prices: dict[str, float]) -> dict:
    """生成当日资产快照。prices: {code: 最新收盘价}。返回快照值。"""
    pos = positions()
    market_value = sum(p["shares"] * prices.get(code, 0.0) for code, p in pos.items())
    cash_now = db.get_cash()
    total = cash_now + market_value
    db.save_snapshot(pd.Timestamp.now().strftime("%Y-%m-%d"), round(cash_now, 2),
                     round(market_value, 2), round(total, 2))
    return {"cash": cash_now, "market_value": market_value, "total": total}