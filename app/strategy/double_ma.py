"""双均线交叉策略（double_ma）。

买入：MA(short) 上穿 MA(long)（金叉）
卖出：MA(short) 下穿 MA(long)（死叉）

对齐参考系统示例：MA5 下穿 MA20 触发卖出（收 1.54）。
"""
import pandas as pd

from app.indicators import ma
from .base import BaseStrategy


class DoubleMA(BaseStrategy):
    code = "double_ma"
    name = "双均线交叉"
    default_params = {"short": 5, "long": 20}

    def warmup(self) -> int:
        return self.params["long"]

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["ma_short"] = ma(out["close"], self.params["short"])
        out["ma_long"] = ma(out["close"], self.params["long"])
        return out

    def signal(self, df: pd.DataFrame, i: int, ctx: dict) -> str | None:
        if i < 1:
            return None
        # 只看 i 及之前，均线在 compute 已算好（用到了 i+1 之前的数据，无未来函数）
        s, l = df["ma_short"], df["ma_long"]
        prev_cross = s.iloc[i - 1] <= l.iloc[i - 1]
        cur_cross = s.iloc[i] > l.iloc[i]
        if prev_cross and cur_cross:
            return "buy"
        if (s.iloc[i - 1] >= l.iloc[i - 1]) and (s.iloc[i] < l.iloc[i]):
            return "sell"
        return None

    def explain(self, df: pd.DataFrame, i: int, side: str) -> str:
        act = "上穿" if side == "buy" else "下穿"
        return f"MA{self.params['short']}{act}MA{self.params['long']}（收 {df['close'].iloc[i]:.2f}）"