"""布林带均值回归策略（boll_mr）。

买入：收盘跌破布林下轨（超卖，博均值回归）
卖出：收盘上穿中轨（回归完成）

对齐参考系统示例：收盘 0.66 跌破布林下轨 0.66（超卖，博均值回归）。
"""
import pandas as pd

from app.indicators import boll
from .base import BaseStrategy


class BollMR(BaseStrategy):
    code = "boll_mr"
    name = "布林带均值回归"
    default_params = {"n": 20, "k": 2.0}

    def warmup(self) -> int:
        return self.params["n"]

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        mid, upper, lower = boll(out["close"], self.params["n"], self.params["k"])
        out["boll_mid"], out["boll_upper"], out["boll_lower"] = mid, upper, lower
        return out

    def signal(self, df: pd.DataFrame, i: int, ctx: dict) -> str | None:
        if i < 1:
            return None
        c = df["close"]
        low = df["boll_lower"]
        mid = df["boll_mid"]
        # 买入：当日收盘跌破下轨（超卖）
        if c.iloc[i] < low.iloc[i]:
            return "buy"
        # 卖出：前一日还在下轨之下（超卖持仓），当日收盘上穿中轨（回归到位）
        if c.iloc[i - 1] <= mid.iloc[i - 1] and c.iloc[i] > mid.iloc[i]:
            return "sell"
        return None

    def explain(self, df: pd.DataFrame, i: int, side: str) -> str:
        c = df["close"].iloc[i]
        if side == "buy":
            return f"收盘 {c:.2f} 跌破布林下轨 {df['boll_lower'].iloc[i]:.2f}（超卖，博均值回归）"
        return f"收盘 {c:.2f} 上穿布林中轨 {df['boll_mid'].iloc[i]:.2f}（回归到位）"