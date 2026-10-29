"""策略推荐：多窗口回测 + 风险调整评分（对齐参考系统的推荐模块）。

评分口径（与参考系统一致）：
    得分 = 近3月/近半年/近1年 平均收益 / max(|近1年最深回撤|, 3%)
    回撤下限 3%：避免"零回撤"把得分放大到失真。

多窗口口径（对齐参考系统"多窗口对比"）：
    指标用全量历史预计算（窗口前的 K 线仅作预热），仿真从窗口起点开始、初始空仓。
    窗口内前 warmup 根不交易（略保守，样本内结果仅供参考）。
"""
from statistics import mean

import pandas as pd

from app.backtest.engine import Fees, run_backtest
from app.backtest.report import compute_metrics
from app.datasource.akshare_source import AkShareSource
from app.datasource.kline_store import get_or_fetch
from app.strategy.registry import STRATEGIES

# 策略风格分类（推荐结论文案用）
CATEGORY = {
    "double_ma": "趋势", "macd": "趋势", "donchian": "趋势",
    "boll_mr": "均值回归", "rsi": "均值回归", "grid": "网格震荡",
}

WINDOWS = [("3m", 90), ("6m", 182), ("1y", 365), ("3y", 1095), ("all", None)]
MIN_BARS = 10  # 窗口内至少这么多根才计算（否则该窗口记 None）


def _slice(df: pd.DataFrame, days: int | None) -> pd.DataFrame:
    if days is None:
        return df
    start = df.index.max() - pd.Timedelta(days=days)
    return df[df.index >= start]


def recommend(cfg: dict, code: str, capital: float = 100000.0) -> dict:
    """对某标的跑 6 策略 × 5 窗口，按风险调整得分排序并给出推荐组合。"""
    df_all = get_or_fetch(code, AkShareSource(), cfg["data"]["kline_dir"])
    if df_all is None or df_all.empty:
        raise ValueError(f"未取到 {code} 的行情数据")

    fees = Fees(**cfg.get("fees", {}))
    rows = []

    for sc, cls in STRATEGIES.items():
        strategy = cls()
        sdf_all = strategy.compute(df_all)      # 全量预计算指标
        row = {"code": sc, "name": strategy.name, "category": CATEGORY.get(sc, "-")}

        for key, days in WINDOWS:
            dfw = _slice(sdf_all, days)
            if len(dfw) < strategy.warmup() + MIN_BARS:
                row[key] = None
                continue
            res = run_backtest(dfw, strategy, capital, fees, code=code)
            m = compute_metrics(res, capital)
            row[key] = m["total_return"]
            if key == "1y":
                row["dd1y"] = m["max_drawdown"]
            if key == "all":
                row["winall"] = m["win_rate"]

        # 风险调整得分
        recent = [row.get("3m"), row.get("6m"), row.get("1y")]
        recent = [x for x in recent if x is not None]
        dd = max(abs(row.get("dd1y") or 0.03), 0.03)   # 回撤下限 3%
        row["score"] = (mean(recent) / dd) if recent else None
        rows.append(row)

    scored = [r for r in rows if r["score"] is not None]
    scored.sort(key=lambda r: r["score"], reverse=True)

    rec = None
    if len(scored) >= 1:
        top1 = scored[0]
        top2 = scored[1] if len(scored) > 1 else None
        combo = [top1["code"]] + ([top2["code"]] if top2 else [])
        avg = mean([x for x in [top1.get("3m"), top1.get("6m"), top1.get("1y")] if x is not None])
        rec = {
            "primary": top1["code"],
            "primary_name": top1["name"],
            "backup": top2["code"] if top2 else None,
            "backup_name": top2["name"] if top2 else None,
            "combo": combo,
            "reason": (
                f"推荐策略：{top1['name']}"
                + (f" + 备选 {top2['name']}" if top2 else "")
                + f"。近3月/半年/1年平均收益 {avg * 100:+.1f}%，"
                f"近1年回撤 {top1.get('dd1y', 0) * 100:.1f}%，"
                f"风险调整后得分最高（{top1['score']:.2f}）。"
                f"当前更适合{top1['category']}型策略"
                + (f"；辅以 {top2['name']} 兼顾其他市况。" if top2 else "。")
                + "（评分=近期平均收益/最深回撤，回撤下限 3%；样本内结果，仅供参考）"
            ),
        }

    return {"code": code, "rows": scored, "recommend": rec,
            "range": {"start": df_all.index[0].strftime("%Y-%m-%d"),
                      "end": df_all.index[-1].strftime("%Y-%m-%d"),
                      "bars": len(df_all)}}