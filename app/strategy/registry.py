"""策略注册表：内置策略 + 用户调参 + 自定义副本，动态实例化。

三层构成（吸收自同类平台的"策略池"设计）：
  1. 内置策略（BUILT_IN）：代码里固化的 9 个，参数取类默认值；
  2. 参数覆盖（strategy_params 表）：调参改的是策略本身的生效参数，
     全局生效（回测、信号、推荐同口径）；
  3. 自定义副本（strategy_custom 表）：另存一条新策略（新 code/新名），
     可与原策略并存、绑定到不同标的。

引擎/CLI/Web 一律通过 get() / all_strategies() 取实例，只认 code。
"""
import json

from .atr_channel import ATRChannel
from .base import BaseStrategy
from .boll_mr import BollMR
from .donchian import Donchian
from .double_ma import DoubleMA
from .grid import Grid
from .kdj import KDJ
from .macd import MACD
from .momentum import Momentum
from .rsi import RSI

# 内置策略（静态注册；副本与参数覆盖动态叠加在其上）
BUILT_IN: dict[str, type[BaseStrategy]] = {
    "double_ma": DoubleMA,
    "macd": MACD,
    "boll_mr": BollMR,
    "donchian": Donchian,
    "rsi": RSI,
    "grid": Grid,
    "kdj": KDJ,
    "momentum": Momentum,
    "atr_channel": ATRChannel,
}

# 兼容旧引用：静态内置表（只读用途；需要含副本/调参时用 all_strategies()）
STRATEGIES = BUILT_IN


def all_strategies() -> dict[str, BaseStrategy]:
    """全部策略实例：内置（应用参数覆盖）+ 自定义副本。

    每次调用重新实例化，保证调参保存后立即生效（回测/信号/推荐同口径）。
    """
    from app import db

    out: dict[str, BaseStrategy] = {}
    for code, cls in BUILT_IN.items():
        out[code] = cls(db.get_strategy_params(code) or None)
    try:
        customs = db.list_strategy_customs()
    except Exception:
        customs = []
    for row in customs:
        base_cls = BUILT_IN.get(row["base"])
        if base_cls is None:
            continue
        try:
            params = json.loads(row["params"])
        except json.JSONDecodeError:
            params = {}
        # 副本自身 params 为建副本时的初始差异；strategy_params 表中的后续调参叠加其上
        merged = {**(params or {}), **db.get_strategy_params(row["code"])}
        inst = base_cls(merged or None)
        inst.code = row["code"]
        inst.name = row["name"]
        inst.base_code = row["base"]
        out[row["code"]] = inst
    return out


def get(code: str) -> BaseStrategy:
    """按 code 取策略实例（含副本与调参生效）。未知 code 抛 KeyError。"""
    inst = all_strategies().get(code)
    if inst is None:
        raise KeyError(f"未知策略: {code}，可选 {sorted(all_strategies())}")
    return inst
