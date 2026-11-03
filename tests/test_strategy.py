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
    # 空仓：锚点=近 20 日最高收盘 100，跌破 -3% → 买入
    closes = [100.0, 100.0, 96.9]
    side, _ = _at("grid", closes)
    assert side == "buy"


def test_grid_anchor_uses_recent_high_not_first_bar():
    # 全历史首日 100，近 20 日横盘 24、现价 23.9（仅 -0.4%）：
    # 正确锚点=24 → 23.9 > 24×0.97 不触发；旧逻辑用首日 100 会误触发
    closes = [100.0] + [24.0] * 39 + [23.9]
    assert _at("grid", closes)[0] is None


def test_grid_anchor_recent_high_breaks_down():
    # 近 20 日最高 24，现价 23.2（-3.3%）→ 相对近期高点触发第 1 档
    closes = [100.0] + [24.0] * 39 + [23.2]
    assert _at("grid", closes)[0] == "buy"


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

# ---------- 新增三策略：KDJ / 动量突破 / ATR 通道 ----------

def _vol_df(closes, pct=0.02):
    """带日内波动的 K 线（高开低收有差异，ATR 才有真实波幅）。"""
    return make_df(closes, high=[c * (1 + pct) for c in closes],
                   low=[c * (1 - pct) for c in closes])


def _scan_all(strategy, closes, with_vol=False):
    """扫描全序列，返回全部信号 [(i, side, close)]。"""
    s = STRATEGIES[strategy]()
    sdf = s.compute(_vol_df(closes) if with_vol else make_df(closes))
    out = []
    for i in range(s.warmup(), len(sdf)):
        side = s.signal(sdf, i, {"cash": 1e6, "shares": 0, "avg_cost": 0, "last_price": None})
        if side:
            out.append((i, side, float(sdf["close"].iloc[i])))
    return out


def test_kdj_buy_on_low_golden_cross():
    """超卖后反弹 → 低位金叉（D<80）买入。"""
    closes = [50.0] * 10 + [50 - i * 1.5 for i in range(1, 12)] + [36.0, 40.0, 44.0]
    sigs = _scan_all("kdj", closes)
    assert any(side == "buy" for _, side, _ in sigs)
    i, side, _ = next(x for x in sigs if x[1] == "buy")
    sdf = STRATEGIES["kdj"]().compute(make_df(closes))
    assert sdf["kdj_d"].iloc[i] < 80          # 金叉不在超买区追高


def test_kdj_sell_on_high_dead_cross():
    """高位滞涨回落 → 高位死叉（D>20）卖出。"""
    closes = [50.0] * 10 + [50 + i * 3.0 for i in range(1, 16)] + [95.0, 91.0, 87.0, 84.0, 82.0]
    sigs = _scan_all("kdj", closes)
    assert any(side == "sell" for _, side, _ in sigs)


def test_kdj_flat_market_no_signal():
    """单边横盘（RSV 恒定无交叉）不触发。"""
    assert _scan_all("kdj", [100.0] * 30) == []


def test_momentum_buy_on_strong_surge():
    closes = [100.0] * 61 + [115.0]
    side, sdf = _at("momentum", closes)
    assert side == "buy"
    assert sdf["mom"].iloc[-1] == pytest.approx(0.15)


def test_momentum_sell_on_breakdown():
    closes = [100.0] * 61 + [95.0]
    side, _ = _at("momentum", closes)
    assert side == "sell"


def test_momentum_idle_in_flat_market():
    side, _ = _at("momentum", [100.0] * 80)
    assert side is None


def test_atr_channel_buy_on_breakout():
    """带波动横盘 → 温和拉升 → 最后一根大跳上穿上轨。"""
    closes = [100.0] * 40 + [101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 125.0]
    s = STRATEGIES["atr_channel"]()
    sdf = s.compute(_vol_df(closes))
    i = len(sdf) - 1
    assert s.signal(sdf, i, {"cash": 1e6, "shares": 0, "avg_cost": 0, "last_price": None}) == "buy"


def test_atr_channel_sell_on_breakdown():
    closes = [100.0] * 40 + [99, 98, 97, 96, 95, 94, 93, 92, 91, 90, 75.0]
    s = STRATEGIES["atr_channel"]()
    sdf = s.compute(_vol_df(closes))
    i = len(sdf) - 1
    assert s.signal(sdf, i, {"cash": 1e6, "shares": 0, "avg_cost": 0, "last_price": None}) == "sell"


def test_atr_channel_flat_no_signal():
    s = STRATEGIES["atr_channel"]()
    sdf = s.compute(_vol_df([100.0] * 60))
    i = len(sdf) - 1
    assert s.signal(sdf, i, {"cash": 1e6, "shares": 0, "avg_cost": 0, "last_price": None}) is None


# ---------- B 批参数开关（回测验证后的可选过滤） ----------

def test_macd_min_dif_filter_blocks_below_zero():
    """min_dif=0：零轴下方金叉不买（默认不过滤照买）。"""
    from app.strategy.macd import MACD
    # 下跌 60 根后反弹：金叉发生在 DIF≈-6.8 的零轴下方
    closes = [120.0 - i * 1.0 for i in range(60)] + [61.0 + i * 0.8 for i in range(8)]
    ctx = {"cash": 1e6, "shares": 0, "avg_cost": 0.0, "last_price": None}

    s = MACD()
    sdf = s.compute(make_df(closes))
    assert any(s.signal(sdf, i, ctx) == "buy" for i in range(s.warmup(), len(sdf)))

    s2 = MACD({"min_dif": 0})
    sdf2 = s2.compute(make_df(closes))
    assert not any(s2.signal(sdf2, i, ctx) == "buy" for i in range(s2.warmup(), len(sdf2)))


def test_rsi_entry_mode_rebound_confirm():
    """entry_mode=1 回升确认：买点从「下穿 30」延后到「回升站上 30」。"""
    from app.strategy.rsi import RSI
    closes = ([100.0 + i * 0.2 for i in range(20)]
              + [103.8 - i * 1.5 for i in range(12)]
              + [87.3 + i * 1.0 for i in range(6)])
    ctx = {"cash": 1e6, "shares": 0, "avg_cost": 0.0, "last_price": None}

    def first_buy(s):
        sdf = s.compute(make_df(closes))
        return next((i for i in range(s.warmup(), len(sdf))
                     if s.signal(sdf, i, ctx) == "buy"), None)

    i0, i1 = first_buy(RSI()), first_buy(RSI({"entry_mode": 1}))
    assert i0 is not None and i1 is not None
    assert i1 > i0          # 确认模式晚于下穿即买


def test_grid_stop_loss_priority():
    """stop_loss=10%：跌破均价 10% 优先止损；默认无止损则按档位继续加仓。"""
    from app.strategy.grid import Grid
    ctx = {"cash": 1e6, "shares": 100, "avg_cost": 100.0,
           "last_price": 100.0, "grid_lots": 1}
    sdf = make_df([100.0, 89.0])
    assert Grid().signal(sdf, 1, ctx) == "buy"                       # 默认：89 触发第 2 档加仓
    assert Grid({"stop_loss": 0.10}).signal(sdf, 1, ctx) == "sell"   # 止损：89 ≤ 100×0.9 清仓
