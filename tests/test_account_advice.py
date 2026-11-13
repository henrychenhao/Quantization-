"""持仓操作建议规则：信号优先 > 深亏 > 回撤 > 浮盈 > 当日异动 > 持有。"""
from app.web import app as webapp


def test_signal_priority_over_loss():
    """有信号时优先给信号建议，即使深亏。"""
    a = webapp._position_advice(-30.0, -5.0, True)
    assert a["level"] == "signal"
    assert "信号" in a["text"]


def test_deep_loss_danger():
    assert webapp._position_advice(-30.0, 0.0, False)["level"] == "danger"


def test_loss_warn():
    assert webapp._position_advice(-18.0, 1.0, False)["level"] == "warn"


def test_profit_take():
    assert webapp._position_advice(8.0, 1.0, False)["level"] == "profit"


def test_day_surge_warn():
    # 浮盈不大但当日大涨 → 异动提示
    assert webapp._position_advice(2.0, 6.5, False)["level"] == "warn"
    assert "大涨" in webapp._position_advice(2.0, 6.5, False)["text"]


def test_day_drop_warn():
    assert webapp._position_advice(2.0, -6.5, False)["level"] == "warn"
    assert "大跌" in webapp._position_advice(2.0, -6.5, False)["text"]


def test_hold_otherwise():
    assert webapp._position_advice(-3.0, 1.0, False)["level"] == "hold"
    assert webapp._position_advice(3.0, 1.0, False)["level"] == "hold"
    assert webapp._position_advice(0.0, None, False)["level"] == "hold"   # 无当日数据