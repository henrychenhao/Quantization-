"""大盘指数：代码判定 / 新浪符号 / 总览 API 组装（打桩数据源，不触网）。"""
import pytest

from app.datasource.akshare_source import INDEX_CODES, is_index, sina_symbol


def test_is_index():
    assert is_index("sh000001") is True       # 上证指数
    assert is_index("sz399001") is True       # 深证成指
    assert is_index("sz399006") is True       # 创业板指
    assert is_index("000977") is False        # 个股（纯代码）
    assert is_index("600519") is False
    assert is_index("513530") is False         # ETF
    assert is_index("") is False
    assert is_index("sh600519") is False       # 带前缀的个股不算指数


def test_sina_symbol_prefix_passthrough():
    """已带市场前缀的代码（指数）原样透传；个股/ETF 前缀规则不受影响。"""
    assert sina_symbol("sh000001") == "sh000001"
    assert sina_symbol("sz399006") == "sz399006"
    assert sina_symbol("600519") == "sh600519"
    assert sina_symbol("000977") == "sz000977"
    assert sina_symbol("513530") == "sh513530"
    assert sina_symbol("159107") == "sz159107"


def test_index_codes_registry():
    assert INDEX_CODES["sh000001"] == "上证指数"
    assert set(INDEX_CODES) == {"sh000001", "sz399001", "sz399006"}


def test_api_market_overview(monkeypatch, tmp_path):
    """总览 API：三指数实时 + 近 90 日走势组装（数据源打桩）。"""
    import pandas as pd

    from app.web import app as webapp

    class _FakeSource:
        def get_spots(self, codes):
            return {"sh000001": {"name": "上证指数", "price": 3500.0, "prev_close": 3450.0,
                                 "change_pct": 1.45}}

    import app.datasource.kline_store as ks
    kline = pd.DataFrame(
        {"close": [10.0, 11.0, 12.0, 13.0]},
        index=pd.to_datetime(["2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04"]),
    )
    monkeypatch.setattr(ks, "load_local", lambda code, d: kline)
    monkeypatch.setattr(webapp, "AkShareSource", _FakeSource)

    d = webapp.api_market_overview()
    assert len(d["items"]) == 3
    sh = d["items"][0]
    assert sh["name"] == "上证指数" and sh["price"] == 3500.0 and sh["change_pct"] == 1.45
    assert [k["close"] for k in sh["kline"]] == [10.0, 11.0, 12.0, 13.0]
    # 无实时行情的指数回落到默认名称
    assert d["items"][1]["name"] == "深证成指"
    assert [k["close"] for k in d["items"][1]["kline"]] == [10.0, 11.0, 12.0, 13.0]
    assert d["items"][1]["price"] is None