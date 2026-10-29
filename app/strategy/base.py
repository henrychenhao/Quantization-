"""策略抽象：统一接口 + 信号数据结构。

signal() 的 ctx（账户上下文，回测与实盘同构）：
    cash        可用现金
    shares      当前持仓股数（0 表示空仓）
    avg_cost    持仓均价（含费）
    last_price  最近一次成交价（None=从未成交，网格锚点回退到区间首日价）

lot_ratio：单笔买入资金占比（默认 1.0 全仓；网格策略按每档占比）。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd


@dataclass
class Signal:
    """一条交易信号。side 取值 'buy' / 'sell'。"""
    code: str
    side: str
    strategy: str
    date: pd.Timestamp
    ref_price: float      # 触发时参考价
    reason: str           # 人类可读的触发理由（推送模板用）


class BaseStrategy(ABC):
    code: str = ""
    name: str = ""
    default_params: dict = {}
    lot_ratio: float = 1.0   # 单笔买入资金占比；grid 等分档策略覆盖此值

    def __init__(self, params: dict | None = None):
        self.params = dict(self.default_params)
        if params:
            self.params.update(params)

    @abstractmethod
    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        """把指标列计算好并合并进 df，返回新 DataFrame。

        引擎在回测循环外一次性预计算，循环内不再重复算，效率高且易学。
        """

    @abstractmethod
    def signal(self, df: pd.DataFrame, i: int, ctx: dict) -> str | None:
        """基于 df 第 i 行（含之前所有行）+ 账户上下文判断，返回 'buy'/'sell' 或 None。

        只允许使用 df.iloc[:i+1] 的数据，杜绝未来函数。
        """

    def warmup(self) -> int:
        """指标预热所需最少 K 线根数（引擎据此跳过前几根）。默认 0。"""
        return 0

    def explain(self, df: pd.DataFrame, i: int, side: str) -> str:
        """信号触发理由（推送/展示用，对齐参考系统如"收盘 47.78 跌破 10 日最低 48.05"）。"""
        return ""