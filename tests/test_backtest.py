"""回测引擎测试：事件驱动、整手、T+1、费用、基准曲线。"""
import pandas as pd
import pytest

from app.backtest.engine import Fees, run_backtest
from app.strategy.base import BaseStrategy
from conftest import make_df


class Dummy(BaseStrategy):
    """可控触发序列的假策略：第 buys/sells 根 K 线返回信号。"""
    code = "dummy"
    name = "测试策略"
    lot_ratio = 1.0

    def __init__(self, buys=(), sells=()):
        self._buys = set(buys)
        self._sells = set(sells)

    def compute(self, df):
        return df

    def signal(self, df, i, ctx):
        if i in self._buys:
            return "buy"
        if i in self._sells:
            return "sell"
        return None


def test_buy_rounds_to_100_lot():
    df = make_df([10.0] * 10)
    r = run_backtest(df, Dummy(buys=(5,)), capital=100000, code="600000")
    buy = [t for t in r.trades if t[1] == "buy"]
    assert len(buy) == 1
    shares = buy[0][3]
    assert shares % 100 == 0
    assert shares > 0
    # 买入不超现金（成交价含滑点与佣金）
    assert buy[0][2] * shares <= 100000


def test_sell_profitable_round_trip():
    # 先平后涨：第 5 根买入 10，第 8 根 12 卖出，应盈利且产生完整往返
    closes = [10.0] * 5 + [12.0] * 5
    r = run_backtest(make_df(closes), Dummy(buys=(4,), sells=(7,)), capital=100000, code="TEST")
    sides = [t[1] for t in r.trades]
    assert sides == ["buy", "sell"]
    final = float(r.equity.iloc[-1])
    assert final > 100000  # 上涨卖出应盈利


def test_no_trade_keeps_equity_at_capital():
    # 不触发任何交易：净值始终等于初始资金（空仓不参与涨跌）
    closes = [10, 11, 12, 11, 10, 12, 13]
    r = run_backtest(make_df(closes), Dummy(), capital=100000, code="TEST")
    assert list(r.trades) == []
    assert (r.equity.astype(float) == 100000.0).all()
    # 基准 = 满仓买入持有
    assert float(r.benchmark_equity.iloc[-1]) == pytest.approx(100000 * 13 / 10)


def test_t1_blocks_same_day_sell_for_stock():
    # A 股股票：当日买入当日不卖（即使有卖出信号）
    closes = [10.0] * 10
    r = run_backtest(make_df(closes), Dummy(buys=(4,), sells=(4,)), capital=100000, code="600000")
    sides = [t[1] for t in r.trades]
    assert sides == ["buy"]  # 同日卖出被 T+1 拦下


def test_etf_can_sell_next_day():
    # ETF（T+0 类）买卖往返完整执行
    closes = [10.0] * 10
    r = run_backtest(make_df(closes), Dummy(buys=(4,), sells=(5,)), capital=100000, code="510300")
    sides = [t[1] for t in r.trades]
    assert sides == ["buy", "sell"]


def test_sell_stock_pays_stamp_tax_but_etf_not():
    fees = Fees()
    amt = 100000.0
    from app.backtest.engine import _fee
    stock_sell = _fee(amt, "sell", etf=False, fees=fees)
    etf_sell = _fee(amt, "sell", etf=True, fees=fees)
    assert stock_sell == pytest.approx(etf_sell + amt * fees.stamp_tax)
    assert etf_sell == pytest.approx(max(amt * fees.commission, fees.min_commission))


def test_min_commission_applies():
    fees = Fees()
    from app.backtest.engine import _fee
    # 小额成交也收最低 5 元佣金
    assert _fee(100.0, "buy", etf=True, fees=fees) == fees.min_commission


def test_start_uses_first_bar_and_benchmark_base():
    closes = [20, 22, 19, 21, 23]
    r = run_backtest(make_df(closes), Dummy(), capital=10000, code="TEST")
    assert float(r.benchmark_equity.iloc[-1]) == pytest.approx(10000 * 23 / 20)


def test_grid_multi_level_accumulates_then_exits():
    """网格多档：逐级加仓 3 档，反弹到均价 +3% 时整仓止盈。"""
    from app.strategy.registry import STRATEGIES
    grid = STRATEGIES["grid"]()
    closes = [100.0] * 10 + [90.0] * 10 + [80.0] * 10 + [70.0] * 5 + [95.0] * 5
    r = run_backtest(make_df(closes), grid, capital=100000, code="TEST")
    buys = [t for t in r.trades if t[1] == "buy"]
    sells = [t for t in r.trades if t[1] == "sell"]
    assert len(buys) == 3   # 空仓第1档 + 2次加仓
    assert len(sells) == 1
    # 每档预算 = 20万 × 20% = 4万，单档股数应约为 40000/成交价（整手）
    assert all(b[3] % 100 == 0 for b in buys)