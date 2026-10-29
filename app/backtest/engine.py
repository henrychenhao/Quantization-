"""回测引擎：事件驱动，逐 K 线回放。

规则与参考系统对齐：
- 信号于当日收盘价成交（实盘"尾盘触发→收盘"同口径），并计入滑点；
- 区间起点前 K 线仅用于指标预热，仿真从起点开始、初始空仓（杜绝前视偏差）；
- 买入按 100 股整手向下取整，卖出允许零股清仓；
- 费用：佣金万2.5（最低 5 元）+ 股票卖出印花税 0.05%（ETF 免）+ 滑点；
- 交易规则按标的打标：跨境 ETF（T+0 类）当日可卖，其余 T+1（隔日可卖）。
"""
from dataclasses import dataclass

import pandas as pd

from app.datasource.base import is_etf


@dataclass
class Fees:
    commission: float = 0.00025
    min_commission: float = 5.0
    stamp_tax: float = 0.0005
    slippage: float = 0.001


@dataclass
class Result:
    code: str
    strategy: str
    start: pd.Timestamp
    end: pd.Timestamp
    equity: pd.Series           # 策略每日净值曲线（index=日期）
    benchmark_equity: pd.Series  # 买入持有基准每日净值
    trades: list                # 成交笔 (date, side, exec_price, shares)


def _fee(amount: float, side: str, etf: bool, fees: Fees) -> float:
    """单边费用 = 佣金 +（卖出股票时的）印花税。ETF 免印花税。"""
    comm = max(amount * fees.commission, fees.min_commission)
    stamp = fees.stamp_tax * amount if (side == "sell" and not etf) else 0.0
    return comm + stamp


def _buy_shares(cash: float, exec_price: float, lot: int, fees: Fees) -> int:
    """估算现金最多可买的整手数（含滑点与佣金近似）。"""
    if exec_price <= 0:
        return 0
    # 粗略：预留约 (1+佣金) 倍资金覆盖手续费
    shares = int((cash / (exec_price * (1 + fees.commission))) // lot * lot)
    return shares


def run_backtest(price_df: pd.DataFrame, strategy, capital: float,
                 fees: Fees | None = None, code: str = "") -> Result:
    fees = fees or Fees()
    etf = is_etf(code)
    df = price_df

    cash = float(capital)
    shares = 0.0
    buy_date: pd.Timestamp | None = None
    trades: list[tuple] = []
    equity: dict = {}
    benchmark: dict = {}

    # 账户上下文：回测与实盘共用同一结构，网格等状态型策略由它推导档位
    ctx = {"cash": capital, "shares": 0.0, "avg_cost": 0.0, "last_price": None,
           "grid_lots": 0, "grid_lot_shares": 0.0}
    multi = getattr(strategy, "multi_lot", False)      # 网格等多档策略：允许已有持仓继续加仓
    first_close = float(df["close"].iloc[0])

    for i in range(len(df)):
        row = df.iloc[i]
        day = row.name
        close = float(row["close"])

        side = strategy.signal(df, i, ctx) if i >= strategy.warmup() else None

        if side == "buy" and (shares <= 0 or multi):
            exec_price = close * (1 + fees.slippage)
            if multi:
                # 多档网格：每档预算 = 初始资金 × 每档占比（不受已持仓影响）
                budget = capital * getattr(strategy, "lot_ratio", 1.0)
            else:
                budget = cash * getattr(strategy, "lot_ratio", 1.0)   # 分档仓位
            lot = _buy_shares(budget, exec_price, 100, fees)
            if lot >= 100:
                amount = lot * exec_price
                fee = _fee(amount, "buy", etf, fees)
                if amount + fee <= cash:
                    cash -= amount + fee
                    shares += lot
                    buy_date = day
                    trades.append((day, "buy", exec_price, lot))
                    if multi:
                        ctx["grid_lots"] = ctx.get("grid_lots", 0) + 1
                        ctx["grid_lot_shares"] = float(lot)
                        ctx["_grid_cost"] = ctx.get("_grid_cost", 0.0) + amount
                        ctx["avg_cost"] = ctx["_grid_cost"] / shares
                    else:
                        ctx["avg_cost"] = exec_price
                    ctx.update(cash=cash, shares=float(shares), last_price=exec_price)

        elif side == "sell" and shares > 0 and (etf or buy_date is None or day > buy_date):
            exec_price = close * (1 - fees.slippage)
            amount = shares * exec_price
            fee = _fee(amount, "sell", etf, fees)
            cash += amount - fee
            trades.append((day, "sell", exec_price, shares))
            ctx.update(cash=cash, shares=0.0, last_price=exec_price,
                       grid_lots=0, grid_lot_shares=0.0, avg_cost=0.0)
            ctx.pop("_grid_cost", None)
            shares = 0.0
            buy_date = None

        equity[day] = cash + shares * close
        benchmark[day] = capital * (close / first_close)

    eq = pd.Series(equity).sort_index()
    bm = pd.Series(benchmark).sort_index()
    return Result(
        code=code, strategy=strategy.code,
        start=eq.index[0], end=eq.index[-1],
        equity=eq, benchmark_equity=bm, trades=trades,
    )