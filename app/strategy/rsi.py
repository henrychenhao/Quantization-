"""RSI 超卖回归策略（rsi）。

买入：RSI(14) 下穿 30（超卖，博反弹）
卖出：RSI(14) 上穿 70（超买，落袋）

参数 entry_mode：0 = 下穿即买（默认）；1 = 回升确认（回到 30 上方才买，滞后 1 根）。
回测结论（8 标的×3 窗口）：回升确认无稳定优势（近 1 年略好、3 年/全历史略差），
打平；默认不开启。

对齐参考系统日志：RSI(14) 下穿 30（当前 29.7，超卖）。
"""
import pandas as pd

from app.indicators import rsi
from .base import BaseStrategy


class RSI(BaseStrategy):
    code = "rsi"
    name = "RSI超卖回归"
    # entry_mode：0 = 下穿超卖线即买（默认）；1 = 回升确认，从下方回到 low 上方才买
    # （滞后 1 根换质量，过滤阴跌中持续超卖的接飞刀）
    default_params = {"n": 14, "low": 30, "high": 70, "entry_mode": 0}

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
        if int(self.params.get("entry_mode", 0)) == 1:
            if r.iloc[i - 1] < low and r.iloc[i] >= low:
                return "buy"    # 回升确认：从超卖区回到 low 上方
        elif r.iloc[i - 1] >= low and r.iloc[i] < low:
            return "buy"        # 下穿超卖线即买
        if r.iloc[i - 1] <= high and r.iloc[i] > high:
            return "sell"       # 上穿超买线
        return None

    def explain(self, df: pd.DataFrame, i: int, side: str) -> str:
        v = df["rsi"].iloc[i]
        if side == "buy":
            if int(self.params.get("entry_mode", 0)) == 1:
                return f"RSI({self.params['n']}) 回升站上 {self.params['low']:.0f}（当前 {v:.1f}，超卖修复确认）"
            return f"RSI({self.params['n']}) 下穿 {self.params['low']:.0f}（当前 {v:.1f}，超卖）"
        return f"RSI({self.params['n']}) 上穿 {self.params['high']:.0f}（当前 {v:.1f}，超买）"