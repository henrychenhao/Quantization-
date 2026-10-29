"""六个策略信号行为测试：构造确定性行情，验证买卖方向与 warmup 边界。"""
import pandas as pd
import pytest

from app.strategy.registry import STRATEGIES
from conftest import make_df


def _scan_signal(strategy, closes, buys=True):
    """扫描整个序列，返回第一个 buy（buys=True）或 sell 信号，无则 None。"""
    s = STRATEGIES[strategy]()
    sdf = s.compute(make_df(closes))
    target = "buy" if buys else "sell"
    for i in range(s.warmup(), len(sdf)):
        if s.signal(sdf, i, {"cash": 1e6, "shares": 0, "avg_cost": 0.0, "last_price": None}) == target:
            return target
    return None


def _at(strategy, closes, ctx=None):
    """在最后一根 K 线触发 signal，返回 (side, sdf)。"""
    s = STRATEGIES[strategy]()
    df = make_df(closes)
    sdf = s.compute(df)
    i = len(sdf) - 1
    if i < s.warmup():
        return None, sdf
    return s.signal(sdf, i, ctx or {"cash": 1e6, "shares": 0, "avg_cost": 0.0, "last_price": None}), sdf


def test_double_ma_golden_cross_buy():
    # 25 根平台后急升 → MA5 上穿 MA20
    closes = [100.0] * 25 + [110.0]
    side, sdf = _at("double_ma", closes)
    assert side == "buy"
    assert sdf["ma_short"].iloc[-1] > sdf["ma_long"].iloc[-1]


def test_double_ma_dead_cross_sell():
    # 高位急跌 → MA5 下穿 MA20
    closes = [100.0] * 25 + [90.0]
    side, _ = _at("double_ma", closes)
    assert side == "sell"


def test_macd_golden_cross_eventually_buys():
    # 先跌后升 → DIF 上穿 DEA（金叉，扫描法定位）
    closes = [80.0 - i for i in range(35)] + [45.0 + i * 2 for i in range(30)]
    s = STRATEGIES["macd"]()
    df = make_df(closes)
    sdf = s.compute(df)
    hit = None
    for i in range(s.warmup(), len(sdf)):
        side = s.signal(sdf, i, {"cash": 1e6, "shares": 0, "avg_cost": 0.0, "last_price": None})
        if side:
            hit = (i, side)
            break
    assert hit is not None and hit[1] == "buy"


def test_boll_mr_buy_when_close_below_lower_band():
    # 30 根平稳后一根深跌 → 跌破下轨触发买入
    closes = [50.0] * 30 + [40.0]
    side, _ = _at("boll_mr", closes)
    assert side == "buy"


def test_donchian_buy_on_breakout():
    # 长时间横盘后突破前 20 日最高
    closes = [50.0] * 25 + [55.0]
    high = [50.0] * 25 + [56.0]
    s = STRATEGIES["donchian"]()
    df = make_df(closes, high=high)
    sdf = s.compute(df)
    side = s.signal(sdf, len(sdf) - 1, {"cash": 1e6, "shares": 0, "avg_cost": 0.0, "last_price": None})
    assert side == "buy"


def test_rsi_buy_on_oversold_cross():
    # 先升后跌：扫描到 RSI 下穿 30 即为买入点
    closes = [100.0 + i for i in range(20)] + [float(100 - i * 2) for i in range(1, 25)]
    side = _scan_signal("rsi", closes, buys=True)
    assert side == "buy"


def test_rsi_sell_on_overbought_cross():
    # 先跌后涨：扫描到 RSI 上穿 70 即为卖出
    closes = [100.0 - i for i in range(20)] + [float(85 + i) for i in range(1, 30)]
    side = _scan_signal("rsi", closes, buys=False)
    assert side == "sell"


def test_grid_buy_when_price_drops_below_anchor():
    # 空仓：锚点=首日收盘 100，跌破 -3% → 买入
    closes = [100.0, 100.0, 96.9]
    side, _ = _at("grid", closes)
    assert side == "buy"


def test_grid_sell_when_price_above_cost():
    # 持仓成本 100，价格涨到 103 以上 → 卖出（已持 1 档）
    closes = [103.0] * 5
    ctx = {"cash": 1e6, "shares": 100, "avg_cost": 100.0, "last_price": 100.0, "grid_lots": 1}
    side, _ = _at("grid", closes, ctx)
    assert side == "sell"


def test_grid_idle_without_price_move():
    closes = [100.0] * 5
    assert _at("grid", closes)[0] is None
    ctx = {"cash": 1e6, "shares": 100, "avg_cost": 100.0, "last_price": 100.0, "grid_lots": 1}
    assert _at("grid", closes, ctx)[0] is None


def test_grid_buys_second_level_after_first_hold():
    # 已持 1 档（锚点 100），再跌 6% → 买第 2 档；仅跌 3% 不重复买
    ctx1 = {"cash": 1e6, "shares": 100, "avg_cost": 97.0, "last_price": 100.0, "grid_lots": 1}
    assert _at("grid", [100.0, 96.9], ctx1)[0] is None          # -3.1% 未到第2档
    assert _at("grid", [100.0, 93.9], ctx1)[0] == "buy"        # -6.1% 触发第2档


def test_grid_full_levels_no_more_buy():
    # 已满 5 档：再跌也不买（等止盈）
    ctx = {"cash": 1e6, "shares": 500, "avg_cost": 90.0, "last_price": 100.0, "grid_lots": 5}
    assert _at("grid", [100.0, 80.0], ctx)[0] is None


def test_warmup_bounds_all_strategies():
    # warmup 内不应触发信号
    for code in STRATEGIES:
        s = STRATEGIES[code]()
        df = make_df([100.0] * 40)
        sdf = s.compute(df)
        ctx = {"cash": 1e6, "shares": 0, "avg_cost": 0.0, "last_price": None}
        for i in range(min(s.warmup(), 5)):
            assert s.signal(sdf, i, ctx) is None, f"{code} 在 warmup 内触发了信号"