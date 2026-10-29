"""唐奇安通道突破策略（donchian）。

买入：收盘突破前 entry 日最高价
卖出：收盘跌破前 exit 日最低价

对齐参考系统示例：收盘 47.78 跌破 10 日最低 48.05（卖出）。
"""
import pandas as pd

from app.indicators import donchian_high, donchian_low
from .base import BaseStrategy


class Donchian(BaseStrategy):
    code = "donchian"
    name = "唐奇安通道突破"
    default_params = {"entry": 20, "exit": 10}

    def warmup(self) -> int:
        return self.params["entry"]

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        # 用 shift(1)：通道取"昨日及之前"的最高/最低，突破当天不参与计算，杜绝未来函数
        out["don_hi"] = donchian_high(out["high"].shift(1), self.params["entry"])
        out["don_lo"] = donchian_low(out["low"].shift(1), self.params["exit"])
        return out

    def signal(self, df: pd.DataFrame, i: int, ctx: dict) -> str | None:
        c = df["close"]
        if c.iloc[i] > df["don_hi"].iloc[i]:
            return "buy"
        if c.iloc[i] < df["don_lo"].iloc[i]:
            return "sell"
        return None

    def explain(self, df: pd.DataFrame, i: int, side: str) -> str:
        c = df["close"].iloc[i]
        if side == "buy":
            return f"收盘 {c:.2f} 突破 {self.params['entry']} 日最高 {df['don_hi'].iloc[i]:.2f}"
        return f"收盘 {c:.2f} 跌破 {self.params['exit']} 日最低 {df['don_lo'].iloc[i]:.2f}"