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


# 触发双均线金叉的行情：25 根横盘后拉升 +8%（避开 +10% 涨停闸门）
_KLINE = make_df([100.0] * 25 + [108.0])


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
    assert any("当日已推送过" in s["reason"] for s in r["skipped"])


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


# ---------- 涨跌停/停牌过滤 ----------

def test_limit_up_blocks_buy_signal(seeded_account, monkeypatch):
    # 主板股涨停（昨收 10 → 涨停 11）：买入信号被过滤
    wid = _seed_watchlist(strategies=("double_ma",))
    closes = [10.0] * 25 + [11.0]  # 大涨触发金叉 + 恰好涨停
    monkeypatch.setattr(engine, "get_or_fetch", lambda *a: make_df(closes))
    r = engine.run_signals(CFG, dry_run=True)
    assert r["generated"] == []
    assert any("涨停" in s["reason"] for s in r["skipped"])


def test_limit_down_blocks_sell_signal(seeded_account, monkeypatch):
    # 持有仓位 + 主板跌停（-10%）：死叉卖出信号被过滤
    wid = _seed_watchlist(code="600519", strategies=("double_ma",))
    db.add_trade(None, "600519", "buy", 100.0, 300, 5.0)
    closes = [100.0] * 25 + [90.0]  # 大跌至跌停，MA5 下穿 MA20
    monkeypatch.setattr(engine, "get_or_fetch", lambda *a: make_df(closes))
    r = engine.run_signals(CFG, dry_run=True)
    assert r["generated"] == []
    assert any("跌停" in s["reason"] for s in r["skipped"])


def test_etf_not_filtered_by_limit(seeded_account, monkeypatch):
    # ETF 不做涨跌停强制过滤
    wid = _seed_watchlist(code="513530", strategies=("double_ma",))
    closes = [1.0] * 25 + [1.1]
    monkeypatch.setattr(engine, "get_or_fetch", lambda *a: make_df(closes))
    r = engine.run_signals(CFG, dry_run=True)
    assert len(r["generated"]) == 1
    assert not any("涨停" in s["reason"] for s in r["skipped"])


def test_spot_limit_up_blocks_buy(seeded_account, monkeypatch):
    # 盘中实时价涨停（昨收 50 → 实时 55，+10%）：唐奇安突破买入被过滤
    wid = _seed_watchlist(code="600519", strategies=("donchian",))
    monkeypatch.setattr(engine, "get_or_fetch", lambda *a: make_df([50.0] * 30))
    monkeypatch.setattr(engine.AkShareSource, "get_spots", _spot_fake(55.0, 50.0))
    r = engine.run_signals(CFG, dry_run=True, spot_mode=True)
    assert r["generated"] == []
    assert any("涨停" in s["reason"] for s in r["skipped"])


# ---------- 盘中轮询（spot_mode） ----------

def _spot_fake(price, prev_close=1.0):
    """构造 get_spots 的替换方法：按调用时的 code 回填。"""
    def _fake(self, codes):
        code = codes[0] if codes else "600519"
        return {code: {"name": "测试", "price": price, "prev_close": prev_close, "change_pct": 0.0}}
    return _fake


def test_spot_mode_uses_intraday_price(seeded_account, monkeypatch):
    # 历史 30 根平稳（qfq 昨收 50），盘中实时价深跌 → 跌破布林下轨，信号价 = 实时价
    wid = _seed_watchlist(strategies=("boll_mr",))
    monkeypatch.setattr(engine, "get_or_fetch", lambda *a: make_df([50.0] * 30))
    monkeypatch.setattr(engine.AkShareSource, "get_spots", _spot_fake(40.0, 50.0))
    r = engine.run_signals(CFG, dry_run=True, spot_mode=True)
    assert len(r["generated"]) == 1
    assert r["generated"][0]["price"] == pytest.approx(40.0)


def test_spot_mode_adjusts_for_dividend_adjustment(seeded_account, monkeypatch):
    """复权口径统一：qfq 昨收 50，未复权昨收 100（10 送 10 后的口径差）→ 实时价应翻倍对齐。"""
    wid = _seed_watchlist(strategies=("boll_mr",))
    monkeypatch.setattr(engine, "get_or_fetch", lambda *a: make_df([50.0] * 30))
    # 未复权昨收 100，实时价 80 → 折算 qfq 口径 = 80 × (50/100) = 40
    monkeypatch.setattr(engine.AkShareSource, "get_spots", _spot_fake(80.0, 100.0))
    r = engine.run_signals(CFG, dry_run=True, spot_mode=True)
    assert len(r["generated"]) == 1
    assert r["generated"][0]["price"] == pytest.approx(40.0)


def test_spot_mode_suspended_price_skipped(seeded_account, monkeypatch):
    # 停牌/无实时价 → 跳过且不落库
    wid = _seed_watchlist(strategies=("boll_mr",))
    monkeypatch.setattr(engine, "get_or_fetch", lambda *a: make_df([50.0] * 30))
    monkeypatch.setattr(engine.AkShareSource, "get_spots", lambda self, codes: {})
    r = engine.run_signals(CFG, dry_run=False, spot_mode=True)
    assert r["generated"] == []
    assert any("实时价" in s["reason"] for s in r["skipped"])
    assert db.list_signals() == []

def test_gate_dedup_ignored_counts(seeded_account):
    """忽略即当日安静：被忽略的信号当天不再重复生成/推送。"""
    _seed_watchlist()
    r1 = engine.run_signals(CFG, dry_run=False)
    assert len(r1["generated"]) == 1
    db.set_signal_status(r1["generated"][0]["id"], "ignored")
    r2 = engine.run_signals(CFG, dry_run=False)
    assert r2["generated"] == []
    assert any("当日已推送过" in s["reason"] for s in r2["skipped"])


# ---------- 信号过期（3 个交易日未跟随自动归档） ----------

from datetime import datetime, timedelta


def _add_pending_days_ago(code, days, strategy="double_ma"):
    """直接插一条 N 天前创建的 pending 信号（绕过当日去重）。"""
    with db.db() as c:
        c.execute(
            "INSERT INTO signal(code,name,side,strategy,reason,ref_price,suggest_shares,created_at,status)"
            " VALUES(?,?,?,?,?,?,?,?, 'pending')",
            (code, "测试股", "buy", strategy, "测试", 10.0, 100,
             (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")),
        )


def test_expire_stale_signals_by_calendar(tmp_path, seeded_account):
    """自定义日历：信号日之后 3 个完整交易日 → 过期。"""
    days = [(datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d") for n in range(2, 12)]
    cal = tmp_path / "cal.txt"
    cal.write_text("\n".join(days), encoding="utf-8")
    _add_pending_days_ago("600519", 9)      # 日历内：之后有 ≥3 个交易日
    _add_pending_days_ago("600000", 3)      # 日历内：之后仅 1 个交易日
    n = db.expire_stale_signals(calendar_path=str(cal))
    assert n == 1
    statuses = {s["code"]: s["status"] for s in db.list_signals()}
    assert statuses["600519"] == "expired"
    assert statuses["600000"] == "pending"


def test_expire_fallback_to_natural_days(seeded_account):
    """日历缺失时退化：自然日 ≥5 天过期。"""
    _add_pending_days_ago("600519", 7)
    _add_pending_days_ago("600000", 2)
    n = db.expire_stale_signals(calendar_path="data/__no_such_cal__.txt")
    assert n == 1
    statuses = {s["code"]: s["status"] for s in db.list_signals()}
    assert statuses["600519"] == "expired"
    assert statuses["600000"] == "pending"


def test_today_signals_never_expire(seeded_account):
    """当日信号不参与过期（当日 K 线未走完）。"""
    _seed_watchlist()
    engine.run_signals(CFG, dry_run=False)      # 今日信号
    assert db.expire_stale_signals(calendar_path="data/__no_such_cal__.txt") == 0
    assert db.list_signals("pending")


# ---------- 合并推送：同标的同向多策略只推一条 ----------

def test_merged_push_single_message_for_multi_strategies(seeded_account, monkeypatch):
    """同一标的同时触发两个买入策略 → 落 2 条信号、只推送 1 条。"""
    calls = []
    monkeypatch.setattr(engine.wecom, "push", lambda title, content, **k: calls.append((title, content)) or "mock")
    # 双均线 + 唐奇安都被同一根大阳线触发金叉/突破
    closes = [100.0] * 25 + [108.0]
    monkeypatch.setattr(engine, "get_or_fetch", lambda *a: make_df(closes))
    wid = _seed_watchlist(code="600519", strategies=("double_ma", "donchian"))
    r = engine.run_signals(CFG, dry_run=False)
    assert len(r["generated"]) == 2                          # 两条信号落库
    assert db.list_signals("pending").__len__() == 2
    assert len(calls) == 1                                   # 只推送一条
    body = calls[0][1]
    assert "2 个策略共振" in body
    assert "双均线交叉" in body and "唐奇安通道突破" in body


def test_single_strategy_push_unaffected(seeded_account, monkeypatch):
    """单策略触发时推送文案仍是单条且含建议量。"""
    calls = []
    monkeypatch.setattr(engine.wecom, "push", lambda title, content, **k: calls.append((title, content)) or "mock")
    _seed_watchlist()
    r = engine.run_signals(CFG, dry_run=False)
    assert len(r["generated"]) == 1
    assert len(calls) == 1
    assert "共振" not in calls[0][1]
    assert "建议" in calls[0][1]
