"""信号质量结算测试：1/5/20 日收益计算 + 策略战绩聚合。"""
import datetime

import pandas as pd
import pytest

from app import db
from app.engine import outcome
from conftest import make_df

CFG = {"data": {"kline_dir": "data/kline"}}

# 信号日（昨天）为第 0 根：后序 10.2/10.1/10.3/10.4/10.5 → 1日+2%、5日+5%
_CLOSES = [10.0, 10.2, 10.1, 10.3, 10.4, 10.5] + [10.5] * 20


def _seed_yesterday_signal(monkeypatch, closes=None, price=10.0, strategy="double_ma", side="buy"):
    """把 created_at 设为昨天，并将本地 K 线打桩为给定序列。"""
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    closes = closes or _CLOSES
    dates = pd.bdate_range(yesterday, periods=len(closes))
    monkeypatch.setattr(outcome, "load_local", lambda *a: make_df(closes, dates=dates))
    sid = db.add_signal("600519", "测试", side, strategy, "触发", price, 800)
    with db.db() as c:
        c.execute("UPDATE signal SET created_at=? WHERE id=?", (yesterday + " 15:00:00", sid))
    return sid


def test_settle_computes_daily_returns(monkeypatch):
    sid = _seed_yesterday_signal(monkeypatch)
    assert outcome.settle_signal_outcomes(CFG) == 1
    sig = db.get_signal(sid)
    assert sig["ret_1d"] == pytest.approx(0.02)
    assert sig["ret_5d"] == pytest.approx(0.05)
    assert sig["ret_20d"] == pytest.approx(0.05)
    assert sig["settled_at"] is not None


def test_settle_skips_today_signal(monkeypatch):
    # 今日信号不结算（当日 K 线未完整）
    sid = db.add_signal("600519", "测试", "buy", "double_ma", "x", 10.0, 800)
    assert outcome.settle_signal_outcomes(CFG) == 0
    assert db.get_signal(sid)["settled_at"] is None


def test_settle_waits_when_kline_insufficient(monkeypatch):
    # 只有信号日一根 K 线（无次日数据）→ 不结算
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    df = make_df([10.0], dates=[pd.Timestamp(yesterday)])
    monkeypatch.setattr(outcome, "load_local", lambda *a: df)
    sid = db.add_signal("600519", "测试", "buy", "double_ma", "x", 10.0, 50)
    with db.db() as c:
        c.execute("UPDATE signal SET created_at=? WHERE id=?", (yesterday + " 15:00:00", sid))
    assert outcome.settle_signal_outcomes(CFG) == 0
    assert db.get_signal(sid)["settled_at"] is None


def test_strategy_stats_aggregates(monkeypatch):
    _seed_yesterday_signal(monkeypatch, strategy="double_ma")
    _seed_yesterday_signal(monkeypatch, strategy="double_ma")
    outcome.settle_signal_outcomes(CFG)
    stats = outcome.strategy_stats()
    assert stats and stats[0]["strategy"] == "double_ma"
    assert stats[0]["count"] == 2
    assert stats[0]["win_rate"] == 1.0        # 两条都涨
    assert stats[0]["avg_1d"] == pytest.approx(0.02)


def test_strategy_stats_mixed_sides(monkeypatch):
    # 买入后涨 = 命中；卖出后涨 = 不命中
    _seed_yesterday_signal(monkeypatch, strategy="macd", side="sell")
    _seed_yesterday_signal(monkeypatch, strategy="macd")
    outcome.settle_signal_outcomes(CFG)
    stats = outcome.strategy_stats()
    macd = next(s for s in stats if s["strategy"] == "macd")
    assert macd["count"] == 2
    assert macd["win_rate"] == 0.5


def test_strategy_stats_empty():
    assert outcome.strategy_stats() == []