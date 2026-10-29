"""信号质量结算：给每个信号算「信号后 1/5/20 个交易日」实际收益。

数据源：本地前复权 K 线（与信号产生口径一致）。由调度器在每日
数据同步后（17:30）调用，只结算已过去的信号，当日信号顺延。

战绩聚合：按策略统计近期已结算信号的命中率与平均收益，供信号中心
展示与 AI 分析引用。
"""
from datetime import datetime

import pandas as pd

from app import db
from app.datasource.kline_store import load_local
from app.strategy.registry import STRATEGIES


def settle_signal_outcomes(cfg: dict) -> int:
    """结算所有可结算的信号，返回本次更新的条数。"""
    today = datetime.now().strftime("%Y-%m-%d")
    kline_dir = cfg["data"]["kline_dir"]
    updated = 0
    for sig in db.unsettled_signals():
        # 当日信号留待次日结算（当日 K 线可能未完成）
        if sig["created_at"][:10] >= today:
            continue
        df = load_local(sig["code"], kline_dir)
        if df is None or df.empty:
            continue
        dates = [d.strftime("%Y-%m-%d") for d in df.index]
        closes = df["close"].tolist()
        # 信号日之后第一个可用的交易日（盘中信号当日无 K 线，落到下一日）
        idx = next((i for i, d in enumerate(dates) if d >= sig["created_at"][:10]), None)
        if idx is None or idx + 1 >= len(closes):
            continue
        base = float(sig["ref_price"]) if sig["ref_price"] else 0.0
        if base <= 0:
            continue

        def ret(n):
            j = idx + n
            return (float(closes[j]) / base - 1.0) if j < len(closes) else None

        r1, r5, r20 = ret(1), ret(5), ret(20)
        if r1 is None and r5 is None and r20 is None:
            continue
        db.set_signal_outcomes(sig["id"], r1, r5, r20, today)
        updated += 1
    return updated


def strategy_stats(limit: int = 20) -> list[dict]:
    """各策略近期信号战绩：次数 / 命中率 / 平均 1·5·20 日收益。

    命中定义：买入信号 ret_5d > 0、卖出信号 ret_5d < 0（赚到差价）。
    只统计已结算（有 ret_5d）的信号，最多取每策略最近 limit 条。
    """
    rows = db.list_signals()
    stats: dict[str, dict] = {}
    for s in rows:
        if s["settled_at"] is None or s["ret_5d"] is None:
            continue
        st = stats.setdefault(s["strategy"], {"count": 0, "wins": 0, "s1": [], "s5": [], "s20": []})
        if st["count"] >= limit:
            continue
        st["count"] += 1
        hit = s["ret_5d"] > 0 if s["side"] == "buy" else s["ret_5d"] < 0
        if hit:
            st["wins"] += 1
        if s["ret_1d"] is not None:
            st["s1"].append(s["ret_1d"])
        if s["ret_5d"] is not None:
            st["s5"].append(s["ret_5d"])
        if s["ret_20d"] is not None:
            st["s20"].append(s["ret_20d"])
    out = []
    for code, st in stats.items():
        cls = STRATEGIES.get(code)
        out.append({
            "strategy": code,
            "name": cls.name if cls else code,
            "count": st["count"],
            "win_rate": round(st["wins"] / st["count"], 3) if st["count"] else 0.0,
            "avg_1d": round(sum(st["s1"]) / len(st["s1"]), 4) if st["s1"] else None,
            "avg_5d": round(sum(st["s5"]) / len(st["s5"]), 4) if st["s5"] else None,
            "avg_20d": round(sum(st["s20"]) / len(st["s20"]), 4) if st["s20"] else None,
        })
    out.sort(key=lambda x: -(x["count"] or 0))
    return out