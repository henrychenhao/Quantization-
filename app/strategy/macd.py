"""MACD 金叉死叉策略。

买入：DIF 上穿 DEA（金叉）
卖出：DIF 下穿 DEA（死叉）

对齐参考系统示例：MACD 死叉: DIF -0.008 / DEA -0.007（收 1.54）。
"""
import pandas as pd

from app.indicators import macd
from .base import BaseStrategy


class MACD(BaseStrategy):
    code = "macd"
    name = "MACD金叉死叉"
    default_params = {"fast": 12, "slow": 26, "signal": 9}

    def warmup(self) -> int:
        return self.params["slow"] + self.params["signal"]

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        dif, dea, hist = macd(out["close"], **self.params)
        out["dif"], out["dea"], out["hist"] = dif, dea, hist
        return out

    def signal(self, df: pd.DataFrame, i: int, ctx: dict) -> str | None:
        if i < 1:
            return None
        d, e = df["dif"], df["dea"]
        if d.iloc[i - 1] <= e.iloc[i - 1] and d.iloc[i] > e.iloc[i]:
            return "buy"
        if d.iloc[i - 1] >= e.iloc[i - 1] and d.iloc[i] < e.iloc[i]:
            return "sell"
        return None

    def explain(self, df: pd.DataFrame, i: int, side: str) -> str:
        act = "金叉" if side == "buy" else "死叉"
        return f"MACD{act}: DIF {df['dif'].iloc[i]:.3f} / DEA {df['dea'].iloc[i]:.3f}（收 {df['close'].iloc[i]:.2f}）"