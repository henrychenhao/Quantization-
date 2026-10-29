"""绩效计算：从回测结果提炼参考系统同口径的绩效八指标，并输出终端报告。

八指标：总收益 / 年化收益 / 超额收益(对买入持有) / 最大回撤 / 夏普比率 /
回合胜率 / 交易笔数 / 期末资产。
"""
import math

import numpy as np
import pandas as pd


def _segment_stats(eq: pd.Series, bm: pd.Series, trades: list, base: float) -> dict:
    """单段绩效：eq 为段内净值，base 为段初资产基准（算收益用）。"""
    final = float(eq.iloc[-1])
    total = final / base - 1.0
    n = len(eq)
    annual = (1.0 + total) ** (252.0 / n) - 1.0 if n > 0 and total > -1 else 0.0
    peak = eq.cummax()
    mdd = float(((eq - peak) / peak).min())
    rets = eq.pct_change().dropna()
    std = float(rets.std())
    sharpe = float(rets.mean()) / std * math.sqrt(252.0) if std > 0 else 0.0
    win = rounds = 0
    buy_price = None
    for _, side, price, _sh in trades:
        if side == "buy":
            buy_price = price
        else:
            rounds += 1
            if buy_price is not None and price > buy_price:
                win += 1
    return {
        "total_return": total,
        "annual_return": annual,
        "max_drawdown": mdd,
        "sharpe": sharpe,
        "n_trades": len(trades),
        "rounds": rounds,
        "win_rate": win / rounds if rounds else 0.0,
        "benchmark_total": float(bm.iloc[-1]) / float(bm.iloc[0]) - 1.0,
        "excess_return": total - (float(bm.iloc[-1]) / float(bm.iloc[0]) - 1.0),
    }


def walk_forward(res, capital: float, oos_ratio: float = 0.3) -> dict | None:
    """样本内/样本外切分绩效（粗检过拟合）。

    把回测区间按时间切分：前 70% 为样本内（策略"见过"的行情），后 30% 为
    样本外（未参与设计的行情）。若样本外收益/夏普显著劣化，警惕过拟合。
    数据不足（<20 交易日）时返回 None。
    """
    eq, bm = res.equity, res.benchmark_equity
    n = len(eq)
    if n < 20 or not (0.0 < oos_ratio < 1.0):
        return None
    cut = int(n * (1.0 - oos_ratio))
    if cut < 10 or n - cut < 5:
        return None
    cut_date = eq.index[cut - 1]
    in_eq, in_bm = eq.iloc[:cut], bm.iloc[:cut]
    # 样本外段含切点当日，保证区间连续、段初资产与样本内期末一致
    oos_eq, oos_bm = eq.iloc[cut - 1:], bm.iloc[cut - 1:]
    in_trades = [t for t in res.trades if t[0] <= cut_date]
    oos_trades = [t for t in res.trades if t[0] > cut_date]
    ins = _segment_stats(in_eq, in_bm, in_trades, capital)
    oos = _segment_stats(oos_eq, oos_bm, oos_trades, float(in_eq.iloc[-1]))
    return {
        "ratio": oos_ratio,
        "cut_date": cut_date.strftime("%Y-%m-%d"),
        "in_days": cut,
        "oos_days": n - cut + 1,
        "in_sample": ins,
        "out_sample": oos,
        "return_decay": ins["total_return"] - oos["total_return"],
    }


def compute_metrics(res, capital: float) -> dict:
    eq = res.equity
    bm = res.benchmark_equity

    final_asset = float(eq.iloc[-1])
    total_return = final_asset / capital - 1.0

    n_days = len(eq)
    annual_return = (1.0 + total_return) ** (252.0 / n_days) - 1.0 if n_days > 0 else 0.0

    benchmark_total = float(bm.iloc[-1]) / capital - 1.0
    excess_return = total_return - benchmark_total

    # 最大回撤
    peak = eq.cummax()
    dd = (eq - peak) / peak
    max_drawdown = float(dd.min())

    # 夏普（日收益年化）
    rets = eq.pct_change().dropna()
    std = float(rets.std())
    sharpe = float(rets.mean()) / std * math.sqrt(252.0) if std > 0 else 0.0

    # 回合胜率：一买一卖记一回合
    win = 0
    rounds = 0
    buy_price = None
    for _, side, price, sh in res.trades:
        if side == "buy":
            buy_price = price
        else:  # sell
            rounds += 1
            if buy_price is not None and price > buy_price:
                win += 1
    win_rate = win / rounds if rounds > 0 else 0.0

    return {
        "total_return": total_return,
        "annual_return": annual_return,
        "benchmark_total": benchmark_total,
        "excess_return": excess_return,
        "max_drawdown": max_drawdown,
        "sharpe": sharpe,
        "win_rate": win_rate,
        "n_trades": len(res.trades),
        "rounds": rounds,
        "final_asset": final_asset,
        "start": res.start,
        "end": res.end,
        "walk_forward": walk_forward(res, capital),
    }


def _pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


def print_report(res, capital: float) -> None:
    m = compute_metrics(res, capital)
    print("=" * 58)
    print(f"回测标的    : {res.code}   策略: {res.strategy}")
    print(f"回测区间    : {m['start'].date()} ~ {m['end'].date()}   {len(res.equity)} 个交易日")
    print(f"初始资金    : ¥{capital:,.0f}")
    print("-" * 58)
    print(f"总收益      : {_pct(m['total_return'])}")
    print(f"年化收益    : {_pct(m['annual_return'])}")
    print(f"基准(买入持有): {_pct(m['benchmark_total'])}   超额: {_pct(m['excess_return'])}")
    print(f"最大回撤    : {_pct(m['max_drawdown'])}")
    print(f"夏普比率    : {m['sharpe']:.2f}")
    print(f"胜率        : {m['win_rate']*100:.0f}% ({m['rounds']} 回合)")
    print(f"交易笔数    : {m['n_trades']}")
    print(f"期末资产    : ¥{m['final_asset']:,.0f}")
    print("=" * 58)