"""KDJ 随机指标策略（kdj）。

买入：K 上穿 D（金叉），且 D < 80 —— 避免在超买区追高（J 极端值过滤）
卖出：K 下穿 D（死叉），且 D > 20 —— 避免在超卖区割肉

捕捉中短期拐点；震荡市敏感，单边强趋势中金叉死叉会频繁反复。
"""
import pandas as pd

from app.indicators import kdj
from .base import BaseStrategy


class KDJ(BaseStrategy):
    code = "kdj"
    name = "KDJ随机指标"
    default_params = {"n": 9, "k_smooth": 3, "d_smooth": 3}

    def warmup(self) -> int:
        # RSV 需 n 根完整窗口 + K/D 平滑收敛余量；ewm 从首根即有值，无需更长
        return self.params["n"] + self.params["k_smooth"] + self.params["d_smooth"] + 3

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        k, d, j = kdj(out["high"], out["low"], out["close"],
                      self.params["n"], self.params["k_smooth"], self.params["d_smooth"])
        out["kdj_k"], out["kdj_d"], out["kdj_j"] = k, d, j
        return out

    def signal(self, df: pd.DataFrame, i: int, ctx: dict) -> str | None:
        if i < 1:
            return None
        k, d = df["kdj_k"], df["kdj_d"]
        golden = k.iloc[i - 1] <= d.iloc[i - 1] and k.iloc[i] > d.iloc[i]
        dead = k.iloc[i - 1] >= d.iloc[i - 1] and k.iloc[i] < d.iloc[i]
        if golden and d.iloc[i] < 80:
            return "buy"
        if dead and d.iloc[i] > 20:
            return "sell"
        return None

    def explain(self, df: pd.DataFrame, i: int, side: str) -> str:
        k, d, j = df["kdj_k"].iloc[i], df["kdj_d"].iloc[i], df["kdj_j"].iloc[i]
        if side == "buy":
            return f"KDJ 金叉：K {k:.1f} 上穿 D {d:.1f}（J {j:.1f}，低位拐头）"
        return f"KDJ 死叉：K {k:.1f} 下穿 D {d:.1f}（J {j:.1f}，高位拐头）"
