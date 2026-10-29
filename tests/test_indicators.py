"""技术指标测试：MA/MACD/BOLL/RSI/Donchian 口径验证。"""
import pandas as pd
import pytest

from app.indicators import boll, donchian_high, donchian_low, ema, ma, macd, rsi


def test_ma_constant_equals_value():
    s = pd.Series([5.0] * 30)
    assert (ma(s, 10).dropna() == 5.0).all()


def test_ma_window_correct():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    m = ma(s, 3)
    assert m.iloc[2] == pytest.approx(2.0)
    assert m.iloc[4] == pytest.approx(4.0)
    assert pd.isna(m.iloc[0]) and pd.isna(m.iloc[1])


def test_ema_first_value_equals_first_input():
    s = pd.Series([10.0, 20.0, 30.0])
    e = ema(s, 3)
    assert e.iloc[0] == pytest.approx(10.0)


def test_macd_zero_for_constant():
    s = pd.Series([10.0] * 40)
    dif, dea, hist = macd(s)
    assert dif.dropna().abs().max() < 1e-9
    assert (hist.dropna().abs() < 1e-9).all()


def test_macd_dif_positive_in_uptrend():
    s = pd.Series([float(i) for i in range(1, 101)])
    dif, _, _ = macd(s)
    assert dif.iloc[-1] > 0
    assert dif.iloc[-1] > dif.iloc[30]


def test_boll_bands_symmetric():
    s = pd.Series([10.0 + (i % 5) for i in range(60)])
    mid, up, low = boll(s, 20, 2.0)
    tail = slice(20, 60)
    assert (up.iloc[tail] >= mid.iloc[tail]).all()
    assert (low.iloc[tail] <= mid.iloc[tail]).all()
    # 上下轨关于中轨对称
    assert (up.iloc[tail] + low.iloc[tail] - 2 * mid.iloc[tail]).abs().max() < 1e-9


def test_rsi_100_for_uptrend_0_for_downtrend():
    up = pd.Series([float(i) for i in range(1, 40)])
    down = pd.Series([float(40 - i) for i in range(1, 40)])
    assert rsi(up).iloc[-1] == pytest.approx(100.0)
    assert rsi(down).iloc[-1] == pytest.approx(0.0)


def test_rsi_bounded():
    import random
    random.seed(7)
    s = pd.Series([random.uniform(1, 100) for _ in range(200)])
    r = rsi(s).dropna()
    assert ((r >= 0) & (r <= 100)).all()


def test_donchian_window():
    s = pd.Series([1.0, 5.0, 3.0, 2.0, 9.0, 1.0, 4.0])
    assert donchian_high(s, 3).iloc[4] == pytest.approx(9.0)
    assert donchian_high(s, 3).iloc[5] == pytest.approx(9.0)
    assert donchian_low(s, 3).iloc[5] == pytest.approx(1.0)