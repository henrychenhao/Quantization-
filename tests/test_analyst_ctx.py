"""AI 上下文扩展测试：量能 / 盘口 / 基本面 / 板块联动。

网络全部打桩（腾讯行情、同花顺行业、K 线获取），不触网。
"""
import sys
import types
from types import SimpleNamespace

import pandas as pd
import pytest

from app import db
from app.datasource import sector, tencent
from app.llm import analyst


def _df(closes, vols):
    n = len(closes)
    dates = pd.bdate_range("2025-01-01", periods=n)
    return pd.DataFrame({
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": vols,
    }, index=pd.DatetimeIndex(dates))


# ---------- 量能上下文（纯本地计算） ----------

def test_volume_ctx_amplify_and_healthy():
    # 近5日均量 ≈ 20日均量 1.58 倍（放量）；近5日涨多跌少且涨日全放量
    closes = [10.0] * 25 + [10.0, 10.2, 10.3, 10.1, 10.4, 10.5]
    vols = [100.0] * 25 + [200, 210, 220, 150, 230, 240]
    s = analyst._volume_ctx(_df(closes, vols))
    assert "放量" in s
    assert "量价配合健康" in s
    assert "今日量比" in s


def test_volume_ctx_shrink_and_divergence():
    # 大幅缩量 + 涨缩量（上攻乏力）→ 背离
    closes = [10.0] * 25 + [10.0, 10.1, 10.0, 9.9, 10.0]
    vols = [100.0] * 25 + [30, 32, 28, 25, 20]
    s = analyst._volume_ctx(_df(closes, vols))
    assert "缩量" in s
    assert "量价背离" in s


def test_volume_ctx_insufficient_data():
    assert analyst._volume_ctx(_df([10.0] * 10, [100] * 10)) == ""


# ---------- 腾讯行情解析（基本面 + 五档） ----------

def test_tencent_parse_quotes(monkeypatch):
    fake = ('v_sz000977="51~浪潮信息~000977~55.38~56.18~55.00~258441~121502~136914'
            '~55.38~11~55.37~10~55.36~32~55.35~2~55.33~8'
            '~55.40~9~55.41~5~55.42~2~55.43~36~55.44~37'
            '~~20261030112921~-0.80~-1.42~56.27~54.51~55.38/258441/1430569931'
            '~258441~143057~1.76~16.72~~56.27~54.51~3.13~812.38~813.24~3.19~61.80~50.56~1.16~"')
    resp = SimpleNamespace(text=fake)
    monkeypatch.setattr(tencent.requests, "get", lambda *a, **k: resp)
    q = tencent.get_quotes(["000977"])
    assert q["000977"]["price"] == pytest.approx(55.38)
    assert q["000977"]["pe"] == pytest.approx(16.72)
    assert q["000977"]["pb"] == pytest.approx(3.19)
    assert q["000977"]["mktcap"] == pytest.approx(813.24)
    assert q["000977"]["bid_vol"] == pytest.approx(11 + 10 + 32 + 2 + 8)
    assert q["000977"]["ask_vol"] == pytest.approx(9 + 5 + 2 + 36 + 37)
    assert q["000977"]["turnover"] == pytest.approx(1.76)


def test_tencent_network_error_swallowed(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("网络抖动")
    monkeypatch.setattr(tencent.requests, "get", boom)
    assert tencent.get_quotes(["000977"]) == {}


# ---------- 同花顺行业指数（含脏数据清洗） ----------

def test_industry_perf_cleans_comma_strings(tmp_path, monkeypatch):
    """接口返回带千分位逗号的字符串收盘价时仍能正确清洗计算。"""
    n = 30
    raw = pd.DataFrame({
        "日期": pd.date_range("2026-06-01", periods=n).strftime("%Y-%m-%d"),
        "收盘价": [f"{1000 + i * 10:,}" for i in range(n)],   # '1,000'、'1,010'…
    })
    fake_ak = types.SimpleNamespace(
        stock_board_industry_index_ths=lambda **k: raw)
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)
    monkeypatch.setattr(sector, "CACHE_DIR", str(tmp_path))
    p = sector.industry_perf("计算机设备")
    assert p is not None
    assert p["ret_1d"] == pytest.approx(1290 / 1280 - 1)
    assert p["ret_5d"] == pytest.approx(1290 / 1240 - 1)
    assert p["ret_20d"] == pytest.approx(1290 / 1090 - 1)


def test_industry_perf_rejects_dirty_data(tmp_path, monkeypatch):
    """收盘价大量缺失（清洗后不足 25 行）→ 返回 None 且不写缓存。"""
    raw = pd.DataFrame({
        "日期": pd.date_range("2026-06-01", periods=30).strftime("%Y-%m-%d"),
        "收盘价": [float("nan")] * 30,
    })
    fake_ak = types.SimpleNamespace(
        stock_board_industry_index_ths=lambda **k: raw)
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)
    monkeypatch.setattr(sector, "CACHE_DIR", str(tmp_path))
    assert sector.industry_perf("计算机设备") is None
    assert list(tmp_path.iterdir()) == []          # 脏数据不入缓存


# ---------- 个股上下文集成（四段注入） ----------

def test_stock_context_injects_four_sections(seeded_account, monkeypatch):
    db.add_watchlist("000977", "浪潮信息", ["macd"], enabled=0)
    db.update_watchlist(db.list_watchlist()[-1]["id"], industry="计算机设备")

    closes = [10.0] * 25 + [10.0, 10.2, 10.3, 10.1, 10.4, 10.5]
    vols = [100.0] * 25 + [200, 210, 220, 150, 230, 240]
    monkeypatch.setattr(analyst, "get_or_fetch", lambda *a, **k: _df(closes, vols))
    monkeypatch.setattr("app.datasource.tencent.get_quotes", lambda codes: {
        "000977": {"name": "浪潮信息", "price": 55.38, "prev_close": 56.18,
                   "bid_vol": 63, "ask_vol": 89, "turnover": 1.76, "pe": 16.72,
                   "pb": 3.19, "mktcap": 813.24, "float_mktcap": 812.38, "vol_ratio": 1.16},
    })
    monkeypatch.setattr("app.datasource.sector.industry_perf", lambda ind: {
        "date": "2026-10-29", "ret_1d": -0.01, "ret_5d": -0.03, "ret_20d": -0.12})

    ctx = analyst.stock_context("000977", {"data": {"kline_dir": "data/kline"}})
    assert "【量能】" in ctx
    assert "【盘口】" in ctx
    assert "【基本面】" in ctx
    assert "【板块】" in ctx
    # 个股近5日涨幅（约 +4%）> 行业 -3% → 强于行业
    assert "强于行业" in ctx


def test_stock_context_etf_skips_fundamental_sector(seeded_account, monkeypatch):
    """ETF：注入量能与盘口，跳过基本面与板块。"""
    db.add_watchlist("513530", "红利ETF", ["double_ma"], enabled=0)
    closes = [1.0] * 30
    vols = [100.0] * 30
    monkeypatch.setattr(analyst, "get_or_fetch", lambda *a, **k: _df(closes, vols))
    monkeypatch.setattr("app.datasource.tencent.get_quotes", lambda codes: {
        "513530": {"name": "红利ETF", "price": 1.0, "prev_close": 1.0,
                   "bid_vol": 100, "ask_vol": 120, "turnover": 0, "pe": 0,
                   "pb": 0, "mktcap": 0, "float_mktcap": 0, "vol_ratio": 0},
    })
    ctx = analyst.stock_context("513530", {"data": {"kline_dir": "data/kline"}})
    assert "【量能】" in ctx
    assert "【盘口】" in ctx
    assert "【基本面】" not in ctx
    assert "【板块】" not in ctx
