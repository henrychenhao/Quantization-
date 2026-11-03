"""ATR 通道突破策略（atr_channel）。

以均线为中轨、±k 倍 ATR 为带宽构建通道：
买入：收盘价上穿 上轨（MA + k×ATR）
卖出：收盘价下穿 下轨（MA - k×ATR）

波动率自适应趋势跟踪：波动大时通道放宽（噪音过滤强），波动小时收紧（反应灵敏）。
"""
import pandas as pd

from app.indicators import atr, ma
from .base import BaseStrategy


class ATRChannel(BaseStrategy):
    code = "atr_channel"
    name = "ATR通道突破"
    default_params = {"ma_n": 20, "atr_n": 14, "k": 2.0}

    def warmup(self) -> int:
        return self.params["ma_n"] + self.params["atr_n"]

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        mid = ma(out["close"], self.params["ma_n"])
        band = self.params["k"] * atr(out["high"], out["low"], out["close"], self.params["atr_n"])
        out["atr_mid"] = mid
        out["atr_up"] = mid + band
        out["atr_dn"] = mid - band
        return out

    def signal(self, df: pd.DataFrame, i: int, ctx: dict) -> str | None:
        if i < 1:
            return None
        up, dn = df["atr_up"], df["atr_dn"]
        close = df["close"]
        if up.iloc[i - 1] >= close.iloc[i - 1] and up.iloc[i] < close.iloc[i]:
            return "buy"    # 上穿上轨
        if dn.iloc[i - 1] <= close.iloc[i - 1] and dn.iloc[i] > close.iloc[i]:
            return "sell"   # 下穿下轨
        return None

    def explain(self, df: pd.DataFrame, i: int, side: str) -> str:
        close = df["close"].iloc[i]
        if side == "buy":
            return (f"收盘 {close:.2f} 上穿 ATR 上轨 {df['atr_up'].iloc[i]:.2f}"
                    f"（中轨 MA{self.params['ma_n']} + {self.params['k']:.0f}×ATR）")
        return (f"收盘 {close:.2f} 下穿 ATR 下轨 {df['atr_dn'].iloc[i]:.2f}"
                f"（中轨 MA{self.params['ma_n']} - {self.params['k']:.0f}×ATR）")
