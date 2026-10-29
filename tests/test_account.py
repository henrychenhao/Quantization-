"""账户闭环测试：记账、持仓/现金推导、快照、错误分支。"""
import pytest

from app import db
from app.account import core as account


def _add_signal(code="600000", side="buy", strategy="double_ma", price=10.0, shares=1000):
    return db.add_signal(code, "测试股", side, strategy, "测试触发", price, shares)


def test_follow_buy_deducts_cash_and_creates_position(seeded_account):
    sid = _add_signal()
    ret = account.follow_signal(sid, price=10.0, shares=1000, fee=7.5)
    assert ret["amount"] == 10000.0
    # 现金 = 100000 - 10000 - 7.5
    assert db.get_cash() == pytest.approx(100000 - 10000 - 7.5)
    pos = account.position_of("600000")
    assert pos["shares"] == 1000
    assert pos["avg_cost"] == pytest.approx((10000 + 7.5) / 1000, abs=1e-4)
    assert db.get_signal(sid)["status"] == "followed"


def test_follow_sell_increases_cash_and_reduces_position(seeded_account):
    # 先买后卖：两笔流水推导出空仓
    bid = _add_signal(side="buy")
    account.follow_signal(bid, price=10.0, shares=1000, fee=7.5)
    sid = _add_signal(side="sell")
    ret = account.follow_signal(sid, price=12.0, shares=1000, fee=8.0)
    assert ret["amount"] == 12000.0
    assert account.position_of("600000") is None
    # 现金 = 100000 - 10007.5 + 12000 - 8
    assert db.get_cash() == pytest.approx(100000 - 10007.5 + 12000 - 8)
    assert len(db.list_trades()) == 2


def test_follow_insufficient_cash_rejected(seeded_account):
    sid = _add_signal()
    with pytest.raises(ValueError, match="现金不足"):
        account.follow_signal(sid, price=200000.0, shares=1, fee=0)


def test_follow_insufficient_position_rejected(seeded_account):
    sid = _add_signal(side="sell")
    with pytest.raises(ValueError, match="持仓不足"):
        account.follow_signal(sid, price=10.0, shares=100, fee=0)


def test_follow_sell_more_than_position_rejected(seeded_account):
    bid = _add_signal(side="buy")
    account.follow_signal(bid, price=10.0, shares=100, fee=0)
    sid = _add_signal(side="sell")
    with pytest.raises(ValueError, match="持仓不足"):
        account.follow_signal(sid, price=10.0, shares=101, fee=0)


def test_follow_non_pending_rejected(seeded_account):
    sid = _add_signal()
    account.follow_signal(sid, price=10.0, shares=100, fee=0)
    with pytest.raises(ValueError, match="仅 pending"):
        account.follow_signal(sid, price=10.0, shares=100, fee=0)


def test_follow_missing_signal_rejected():
    with pytest.raises(ValueError, match="不存在"):
        account.follow_signal(99999, price=10.0, shares=100, fee=0)


def test_snapshot_persists_row(seeded_account):
    bid = _add_signal(side="buy")
    account.follow_signal(bid, price=10.0, shares=1000, fee=5)
    snap = account.snapshot({"600000": 11.0})
    assert snap["market_value"] == pytest.approx(11000.0)
    rows = db.list_snapshots(10)
    assert len(rows) == 1
    assert float(rows[0]["total"]) == pytest.approx(snap["total"])


def test_position_average_cost_includes_buy_fee(seeded_account):
    bid = _add_signal(side="buy")
    account.follow_signal(bid, price=10.0, shares=1000, fee=10.0)
    pos = account.position_of("600000")
    assert pos["avg_cost"] == pytest.approx(10.01)


# ---------- 流水纠错（update_trade） ----------

def _seed_buy():
    """买 1000 股 @10（费7.5），返回 trade id。"""
    sid = _add_signal()
    account.follow_signal(sid, price=10.0, shares=1000, fee=7.5)
    return db.list_trades()[0]["id"]


def test_update_trade_recomputes_cash_and_position(seeded_account):
    tid = _seed_buy()
    # 修正买入价为 12：现金与持仓成本应重新推导
    r = account.update_trade(tid, price=12.0, shares=1000, fee=7.5)
    assert r["cash"] == pytest.approx(100000 - 12000 - 7.5)
    pos = account.position_of("600000")
    assert pos["avg_cost"] == pytest.approx(12.0075, abs=1e-4)


def test_update_trade_change_shares(seeded_account):
    tid = _seed_buy()
    account.update_trade(tid, price=10.0, shares=500, fee=5.0)
    assert account.position_of("600000")["shares"] == 500
    assert db.get_cash() == pytest.approx(100000 - 5000 - 5.0)


def test_update_trade_reject_negative_cash(seeded_account):
    tid = _seed_buy()
    with pytest.raises(ValueError, match="现金为负"):
        account.update_trade(tid, price=200000.0, shares=1000, fee=0)
    # 拒绝后数据不变
    assert db.get_cash() == pytest.approx(100000 - 10000 - 7.5)


def test_update_trade_reject_negative_position(seeded_account):
    bid = _add_signal(side="buy")
    account.follow_signal(bid, price=10.0, shares=500, fee=5.0)
    # 先把 500 股卖光，再把买入流水改小数量 → 持仓为负
    sid = _add_signal(side="sell")
    account.follow_signal(sid, price=11.0, shares=500, fee=5.0)
    tid = db.list_trades()[0]["id"]  # 最新一条 = 卖出流水
    with pytest.raises(ValueError, match="持仓为负"):
        account.update_trade(tid, price=11.0, shares=600, fee=5.0)


def test_update_trade_missing_rejected():
    with pytest.raises(ValueError, match="流水不存在"):
        account.update_trade(99999, price=10.0, shares=100, fee=0)


def test_update_trade_invalid_values_rejected(seeded_account):
    tid = _seed_buy()
    with pytest.raises(ValueError):
        account.update_trade(tid, price=0, shares=100, fee=0)
    with pytest.raises(ValueError):
        account.update_trade(tid, price=10.0, shares=-5, fee=0)
    with pytest.raises(ValueError):
        account.update_trade(tid, price=10.0, shares=100, fee=-1)