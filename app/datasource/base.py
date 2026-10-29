"""行情数据源抽象：所有数据源实现同一接口，上层不感知具体来源。"""
from abc import ABC, abstractmethod

import pandas as pd


class DataSource(ABC):
    """统一行情数据源接口。

    主源 AkShare、备源各自实现本接口，切换数据源不动业务代码。
    返回的 K 线统一为：date 作索引的 DataFrame，列为
    open / high / low / close / volume / amount。
    """

    @abstractmethod
    def get_daily(self, code: str, start: str | None = None, end: str | None = None) -> pd.DataFrame:
        """获取日 K 线（前复权）。start/end 为 'YYYY-MM-DD'，None 表示不限。"""

    @abstractmethod
    def get_spot(self, code: str) -> float:
        """获取实时快照价（P1 实时引擎使用，P0 可先抛 NotImplementedError）。"""


def is_etf(code: str) -> bool:
    """按 A 股代码段粗判标的类型：51/56/58/15/16 开头为场内 ETF 或基金，其余按个股。

    用于选择数据接口与费率规则（ETF 免印花税；跨境 ETF 可 T+0）。
    参考系统自选池大量为 ETF（513530/159107/159740/515050），故 P0 以 ETF 为主链路。
    """
    return code.startswith(("51", "56", "58", "15", "16", "50", "52"))