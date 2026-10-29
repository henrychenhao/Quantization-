"""RSI 超卖回归策略（rsi）。

买入：RSI(14) 下穿 30（超卖，博反弹）
卖出：RSI(14) 上穿 70（超买，落袋）

对齐参考系统日志：RSI(14) 下穿 30（当前 29.7，超卖）。
"""
import pandas as pd

from app.indicators import rsi
from .base import BaseStrategy


class RSI(BaseStrategy):
    code = "rsi"
    name = "RSI超卖回归"
    default_params = {"n": 14, "low": 30, "high": 70}

    def warmup(self) -> int:
        return self.params["n"] + 1

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["rsi"] = rsi(out["close"], self.params["n"])
        return out

    def signal(self, df: pd.DataFrame, i: int, ctx: dict) -> str | None:
        if i < 1:
            return None
        r = df["rsi"]
        low, high = self.params["low"], self.params["high"]
        if r.iloc[i - 1] >= low and r.iloc[i] < low:
            return "buy"    # 下穿超卖线
        if r.iloc[i - 1] <= high and r.iloc[i] > high:
            return "sell"   # 上穿超买线
        return None

    def explain(self, df: pd.DataFrame, i: int, side: str) -> str:
        v = df["rsi"].iloc[i]
        if side == "buy":
            return f"RSI({self.params['n']}) 下穿 {self.params['low']:.0f}（当前 {v:.1f}，超卖）"
        return f"RSI({self.params['n']}) 上穿 {self.params['high']:.0f}（当前 {v:.1f}，超买）"