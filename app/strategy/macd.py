"""MACD 金叉死叉策略。

买入：DIF 上穿 DEA（金叉）
卖出：DIF 下穿 DEA（死叉）

参数 min_dif：买入要求 DIF > 该值，默认 -999999 不限制；设为 0 = 仅零轴上方金叉买入。
回测结论（8 标的×3 窗口）：零轴过滤是「用收益换回撤」——平均回撤改善 10~20pp，
但长期收益显著下降（全历史 +198%→+49%）；适合震荡市/低风险偏好，默认不开启。

对齐参考系统示例：MACD 死叉: DIF -0.008 / DEA -0.007（收 1.54）。
"""
import pandas as pd

from app.indicators import macd
from .base import BaseStrategy


class MACD(BaseStrategy):
    code = "macd"
    name = "MACD金叉死叉"
    # min_dif：买入要求 DIF > 该值。默认 -999999 不限制；设为 0 = 只在零轴上方
    # 金叉买入（过滤下跌中继的假金叉，回测验证后生效）
    default_params = {"fast": 12, "slow": 26, "signal": 9, "min_dif": -999999.0}

    def warmup(self) -> int:
        return self.params["slow"] + self.params["signal"]

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        # 传给指标计算的仅均线参数（min_dif 是信号过滤项，不属于指标计算）
        p = {k: self.params[k] for k in ("fast", "slow", "signal")}
        dif, dea, hist = macd(out["close"], **p)
        out["dif"], out["dea"], out["hist"] = dif, dea, hist
        return out

    def signal(self, df: pd.DataFrame, i: int, ctx: dict) -> str | None:
        if i < 1:
            return None
        d, e = df["dif"], df["dea"]
        if d.iloc[i - 1] <= e.iloc[i - 1] and d.iloc[i] > e.iloc[i]:
            if d.iloc[i] > float(self.params.get("min_dif", -999999.0)):
                return "buy"
            return None
        if d.iloc[i - 1] >= e.iloc[i - 1] and d.iloc[i] < e.iloc[i]:
            return "sell"
        return None

    def explain(self, df: pd.DataFrame, i: int, side: str) -> str:
        act = "金叉" if side == "buy" else "死叉"
        return f"MACD{act}: DIF {df['dif'].iloc[i]:.3f} / DEA {df['dea'].iloc[i]:.3f}（收 {df['close'].iloc[i]:.2f}）"