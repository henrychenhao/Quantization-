"""动量突破策略（momentum）。

买入：N 日动量（现价 / N 日前价 - 1）≥ 买入阈值（默认 +8%）
卖出：N 日动量 ≤ 卖出阈值（默认 -3%）

捕捉趋势惯性：动量一旦形成短期难以逆转；横盘市动量归零不触发。
"""
import pandas as pd

from .base import BaseStrategy


class Momentum(BaseStrategy):
    code = "momentum"
    name = "动量突破"
    default_params = {"n": 60, "buy_th": 0.08, "sell_th": -0.03}

    def warmup(self) -> int:
        return self.params["n"] + 1

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["mom"] = out["close"] / out["close"].shift(self.params["n"]) - 1.0
        return out

    def signal(self, df: pd.DataFrame, i: int, ctx: dict) -> str | None:
        mom = df["mom"].iloc[i]
        if pd.isna(mom):
            return None
        if mom >= self.params["buy_th"]:
            return "buy"
        if mom <= self.params["sell_th"]:
            return "sell"
        return None

    def explain(self, df: pd.DataFrame, i: int, side: str) -> str:
        mom = df["mom"].iloc[i]
        if side == "buy":
            return (f"{self.params['n']}日动量 +{mom * 100:.1f}% "
                    f"突破买入阈值 +{self.params['buy_th'] * 100:.0f}%（趋势惯性启动）")
        return (f"{self.params['n']}日动量 {mom * 100:.1f}% "
                f"跌破卖出阈值 {self.params['sell_th'] * 100:.0f}%（趋势走坏）")
