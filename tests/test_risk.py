"""组合级风控（闸门5）测试：总仓位 / 单标的 / 现金预留。

风控仅作用于买入信号；risk 段全部为 0 或缺失时保持原有行为。
"""
import pytest

from app import db
from app.engine import signals as engine
from conftest import make_df

CFG = {
    "fees": {"commission": 0.00025, "min_commission": 5.0, "stamp_tax": 0.0005, "slippage": 0.001},
    "data": {"kline_dir": "data/kline"},
    "push": {"channel": "mock"},
    "risk": {"max_position_ratio": 0.8, "max_single_ratio": 0.4, "min_cash_ratio": 0.05},
}

_KLINE = make_df([100.0] * 25 + [108.0])


@pytest.fixture(autouse=True)
def stub_network(monkeypatch):
    monkeypatch.setattr(engine, "get_or_fetch", lambda code, source, d: _KLINE)
    monkeypatch.setattr(engine.wecom, "push", lambda *a, **k: "mock")
    monkeypatch.setattr(engine.wecom, "init", lambda *a, **k: None)


# ---------- 直接：_combo_risk_check ----------

def test_no_risk_config_never_blocks(seeded_account):
    assert engine._combo_risk_check("600519", "buy", 50.0, 100, 5.0, {}, "data/kline", 100000) is None
    assert engine._combo_risk_check(
        "600519", "buy", 50.0, 100, 5.0,
        {"max_position_ratio": 0, "max_single_ratio": 0, "min_cash_ratio": 0},
        "data/kline", 100000) is None


def test_sell_never_blocked_by_risk(seeded_account):
    assert engine._combo_risk_check("600519", "sell", 50.0, 100, 5.0,
                                    CFG["risk"], "data/kline", 100000) is None


def test_max_position_boundary(seeded_account, monkeypatch):
    # 组合市值 90000 + 现金 100000 → 总资产 190000
    monkeypatch.setattr(engine, "_portfolio_market_value", lambda d: 90000.0)
    # 买 30000 → (90000+30000)/190000 ≈ 63% ≤ 80% 放行
    assert engine._combo_risk_check("600519", "buy", 10.0, 3000, 10.0,
                                    CFG["risk"], "data/kline", 100000) is None
    # 买 160000 → (90000+160000)/190000 ≈ 132% > 80% 拦截
    r = engine._combo_risk_check("600519", "buy", 10.0, 16000, 10.0,
                                 CFG["risk"], "data/kline", 100000)
    assert r and "组合级风控" in r and "仓位" in r


def test_max_single_with_existing_position(seeded_account, monkeypatch):
    # 已持有 600519 共 3000 股（成本 8 元）；组合其余市值 0
    db.add_trade(None, "600519", "buy", 8.0, 3000, 5.0)
    monkeypatch.setattr(engine, "_portfolio_market_value", lambda d: 0.0)
    # 现价 10：已持市值 30000。再买 1000 股（10000）→ 40000/100000 = 40% ≤ 40% 放行
    assert engine._combo_risk_check("600519", "buy", 10.0, 1000, 10.0,
                                    CFG["risk"], "data/kline", 100000) is None
    # 再买 2000 股（20000）→ 50000/100000 = 50% > 40% 拦截
    r = engine._combo_risk_check("600519", "buy", 10.0, 2000, 10.0,
                                 CFG["risk"], "data/kline", 100000)
    assert r and "单标的" in r


def test_min_cash_reserve(seeded_account, monkeypatch):
    monkeypatch.setattr(engine, "_portfolio_market_value", lambda d: 0.0)
    # 仅现金预留生效（放开仓位与单标的），排除其他闸门干扰
    cash_risk = {"max_position_ratio": 1.0, "max_single_ratio": 1.0, "min_cash_ratio": 0.05}
    # 预留 5% × 10万 = 5000。买 90000 → 剩 10000 ≥ 5000 放行
    assert engine._combo_risk_check("600519", "buy", 10.0, 9000, 0.0,
                                    cash_risk, "data/kline", 100000) is None
    # 买 98000 → 剩 2000 < 5000 拦截
    r = engine._combo_risk_check("600519", "buy", 10.0, 9800, 0.0,
                                 cash_risk, "data/kline", 100000)
    assert r and "现金" in r


# ---------- 集成：run_signals 接入 ----------

def test_run_signals_blocks_by_combo_risk(seeded_account, monkeypatch):
    """组合市值 15 万 + 满现金 10 万：买入 9.7 万后总仓位 ≈ 99% 超 80% → 拦截。"""
    db.add_watchlist("600519", "测试股", ["double_ma"])
    monkeypatch.setattr(engine, "_portfolio_market_value", lambda d: 150000.0)
    r = engine.run_signals(CFG, dry_run=True)
    assert r["generated"] == []
    assert any("组合级风控" in s["reason"] for s in r["skipped"])


def test_run_signals_passes_when_within_limits(seeded_account, monkeypatch):
    """组合市值 0、现金 10 万：买 ~9.7 万 → 仓位 97% > 80% 也会拦。
    用宽松 risk（上限 1.0）验证放行路径。"""
    db.add_watchlist("600519", "测试股", ["double_ma"])
    monkeypatch.setattr(engine, "_portfolio_market_value", lambda d: 0.0)
    loose = {**CFG, "risk": {"max_position_ratio": 1.0, "max_single_ratio": 1.0, "min_cash_ratio": 0.0}}
    r = engine.run_signals(loose, dry_run=True)
    assert len(r["generated"]) == 1
    assert not any("组合级风控" in s["reason"] for s in r["skipped"])