"""网格交易策略（grid）——多档并存版。

参考系统参数口径：买入步距 3% / 卖出步距 3% / 档数 5 / 每档资金占比 20% / 底仓 0%。

实现：
- 每档资金 = 初始资金 × (1/n_levels)，回测引擎按此预算买入一档并累计持仓；
- 档位状态完全由账户上下文 ctx 推导（回测与实盘共用）：
    ctx["grid_lots"]        当前已持有的档数（回测由引擎维护 / 实盘由持仓估算）
- 买入：持有档数 < n_levels 且现价 <= 锚点 × (1 - 买入步距×(档数+1)) → 追加一档；
- 卖出：现价 >= 持仓均价 × (1 + 卖出步距) → 整仓止盈（引擎清仓）。

锚点 = 最近一次成交价（未成交时取区间首日收盘价）。
"""
import pandas as pd

from .base import BaseStrategy


class Grid(BaseStrategy):
    code = "grid"
    name = "网格交易"
    default_params = {"buy_step": 0.03, "sell_step": 0.03, "n_levels": 5}
    multi_lot = True   # 允许已有持仓时继续加仓（多档并存）

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        # 每档资金占比 = 1/n_levels（以初始资金为基准）
        self.lot_ratio = 1.0 / self.params["n_levels"]

    def warmup(self) -> int:
        return 1

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        return df  # 网格不需要技术指标

    def signal(self, df: pd.DataFrame, i: int, ctx: dict) -> str | None:
        price = float(df["close"].iloc[i])
        buy_step = self.params["buy_step"]
        sell_step = self.params["sell_step"]
        n_levels = self.params["n_levels"]

        # 当前已持有档数：优先 ctx（回测由引擎维护），空仓强制归零
        levels = int(ctx.get("grid_lots", 0)) if ctx["shares"] > 0 else 0

        anchor = ctx["last_price"] if ctx["last_price"] else float(df["close"].iloc[0])

        # 未满档且价格跌到"下一档"买入位 → 加仓（第 levels+1 档）
        if levels < n_levels and price <= anchor * (1 - buy_step * (levels + 1)):
            return "buy"
        # 持仓浮盈到均价 + 卖出步距 → 止盈（引擎整仓清空）
        if levels > 0 and ctx["avg_cost"] > 0 and price >= ctx["avg_cost"] * (1 + sell_step):
            return "sell"
        return None

    def explain(self, df: pd.DataFrame, i: int, side: str) -> str:
        price = float(df["close"].iloc[i])
        if side == "buy":
            return (f"网格买入档触发（现价 {price:.2f} "
                    f"跌破锚点 -{self.params['buy_step']*100:.0f}%）")
        return (f"网格卖出档触发（现价 {price:.2f} "
                f"达到持仓均价 +{self.params['sell_step']*100:.0f}%）")