"""信号引擎测试：四道闸门（价格/资金/持仓/去重）+ 正常生成。

数据源与推送全部打桩，不触网、不推真消息。
"""
import pytest

from app import db
from app.engine import signals as engine
from conftest import make_df

CFG = {
    "fees": {"commission": 0.00025, "min_commission": 5.0, "stamp_tax": 0.0005, "slippage": 0.001},
    "data": {"kline_dir": "data/kline"},
    "push": {"channel": "mock"},
}


@pytest.fixture(autouse=True)
def stub_network(monkeypatch):
    """替换行情获取与推送，避免任何真实网络调用。"""
    monkeypatch.setattr(engine, "get_or_fetch", lambda code, source, d: _KLINE)
    monkeypatch.setattr(engine.wecom, "push", lambda *a, **k: "mock")
    monkeypatch.setattr(engine.wecom, "init", lambda *a, **k: None)


# 触发双均线金叉的行情：25 根横盘后大涨
_KLINE = make_df([100.0] * 25 + [110.0])


def _seed_watchlist(code="600519", strategies=("double_ma",)):
    db.add_watchlist(code, "测试股", list(strategies))
    return db.list_watchlist()[-1]["id"]


def test_generate_buy_signal(seeded_account):
    _seed_watchlist()
    r = engine.run_signals(CFG, dry_run=True)
    assert len(r["generated"]) == 1
    sig = r["generated"][0]
    assert sig["side"] == "buy"
    assert sig["strategy"] == "double_ma"
    assert sig["shares"] >= 100
    # dry_run 不落库
    assert db.list_signals() == []


def test_generate_writes_db_and_keeps_cash(seeded_account):
    _seed_watchlist()
    r = engine.run_signals(CFG, dry_run=False)
    assert len(r["generated"]) == 1
    rows = db.list_signals()
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    # 信号不入账：现金不变
    assert db.get_cash() == seeded_account


def test_gate_dedup_skips_second_signal(seeded_account):
    _seed_watchlist()
    engine.run_signals(CFG, dry_run=False)
    r = engine.run_signals(CFG, dry_run=False)
    assert len(r["generated"]) == 0
    assert any("当日已有信号" in s["reason"] for s in r["skipped"])


def test_gate_bad_price_skipped(seeded_account, monkeypatch):
    _seed_watchlist()
    monkeypatch.setattr(engine, "get_or_fetch", lambda *a: make_df([0.0] * 30))
    r = engine.run_signals(CFG, dry_run=True)
    assert len(r["generated"]) == 0
    assert any("价格异常" in s["reason"] for s in r["skipped"])


def test_gate_insufficient_cash_skipped(seeded_account, monkeypatch):
    _seed_watchlist()
    db.set_cash(100.0)  # 现金不足一手
    r = engine.run_signals(CFG, dry_run=True)
    assert len(r["generated"]) == 0
    assert any("资金不足" in s["reason"] for s in r["skipped"])


def test_gate_sell_without_position_skipped(seeded_account, monkeypatch):
    wid = _seed_watchlist()
    # 布林均值回归反弹到位 → 卖信号，但无持仓 → 跳过
    db.update_watchlist(wid, strategies=["boll_mr"])
    closes = [50.0] * 30 + [40.0, 49.0, 50.0]  # 深跌反弹回中轨 → 最后一根卖
    monkeypatch.setattr(engine, "get_or_fetch", lambda *a: make_df(closes))
    r = engine.run_signals(CFG, dry_run=True)
    assert len(r["generated"]) == 0
    assert any("无持仓" in s["reason"] for s in r["skipped"])


def test_sell_signal_caps_shares_at_position(seeded_account, monkeypatch):
    # 有持仓时卖信号应建议卖出全部持仓
    wid = _seed_watchlist()
    db.add_trade(None, "600519", "buy", 50.0, 300, 5.0)
    db.update_watchlist(wid, strategies=["boll_mr"])
    closes = [50.0] * 30 + [40.0, 49.0, 50.0]
    monkeypatch.setattr(engine, "get_or_fetch", lambda *a: make_df(closes))
    r = engine.run_signals(CFG, dry_run=True)
    sells = [g for g in r["generated"] if g["side"] == "sell"]
    assert sells and sells[0]["shares"] == 300


def test_disabled_watchlist_not_scanned(seeded_account):
    db.add_watchlist("600519", "测试股", ["double_ma"], enabled=0)
    r = engine.run_signals(CFG, dry_run=True)
    assert r["generated"] == []
    assert r["watch"] == 0


# ---------- 盘中轮询（spot_mode） ----------

def test_spot_mode_uses_intraday_price(seeded_account, monkeypatch):
    # 历史 30 根平稳，盘中实时价深跌 → 布林跌破下轨，信号价 = 实时价
    wid = _seed_watchlist(strategies=("boll_mr",))
    monkeypatch.setattr(engine, "get_or_fetch", lambda *a: make_df([50.0] * 30))
    monkeypatch.setattr(engine.AkShareSource, "get_spot", lambda self, code: 40.0)
    r = engine.run_signals(CFG, dry_run=True, spot_mode=True)
    assert len(r["generated"]) == 1
    assert r["generated"][0]["price"] == pytest.approx(40.0)


def test_spot_mode_suspended_price_skipped(seeded_account, monkeypatch):
    # 停牌/无实时价 → 跳过且不落库
    wid = _seed_watchlist(strategies=("boll_mr",))
    monkeypatch.setattr(engine, "get_or_fetch", lambda *a: make_df([50.0] * 30))
    monkeypatch.setattr(engine.AkShareSource, "get_spot", lambda self, code: 0.0)
    r = engine.run_signals(CFG, dry_run=False, spot_mode=True)
    assert r["generated"] == []
    assert any("实时价" in s["reason"] for s in r["skipped"])
    assert db.list_signals() == []