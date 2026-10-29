"""策略注册表：把策略类按 code 登记，引擎/CLI/Web 只认 code。"""
from .base import BaseStrategy
from .boll_mr import BollMR
from .donchian import Donchian
from .double_ma import DoubleMA
from .grid import Grid
from .macd import MACD
from .rsi import RSI

STRATEGIES: dict[str, type[BaseStrategy]] = {
    "double_ma": DoubleMA,
    "macd": MACD,
    "boll_mr": BollMR,
    "donchian": Donchian,
    "rsi": RSI,
    "grid": Grid,
}


def get(code: str) -> BaseStrategy:
    if code not in STRATEGIES:
        raise KeyError(f"未知策略: {code}，可选 {list(STRATEGIES)}")
    return STRATEGIES[code]()