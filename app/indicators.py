"""技术指标库（自研，避免 TA-Lib 编译依赖）。

全部输入输出为 pandas Series / DataFrame，指标口径可控、可审计。
P0 覆盖六个策略所需的全部指标：MA / MACD / BOLL / RSI / Donchian。
"""
import pandas as pd


def ma(s: pd.Series, n: int) -> pd.Series:
    """简单移动平均"""
    return s.rolling(n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    """指数移动平均"""
    return s.ewm(span=n, adjust=False).mean()


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD 三线。返回 (dif, dea, hist)。"""
    dif = ema(close, fast) - ema(close, slow)
    dea = ema(dif, signal)
    hist = (dif - dea) * 2
    return dif, dea, hist


def boll(close: pd.Series, n: int = 20, k: float = 2.0):
    """布林带。返回 (mid, upper, lower)。"""
    mid = ma(close, n)
    std = close.rolling(n).std()
    return mid, mid + k * std, mid - k * std


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """RSI，采用 Wilder 平滑（同常见行情软件口径）。"""
    delta = close.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    # alpha=1/n 即 Wilder 的平滑方式
    up_avg = up.ewm(alpha=1 / n, adjust=False).mean()
    down_avg = down.ewm(alpha=1 / n, adjust=False).mean()
    rs = up_avg / down_avg
    return 100 - 100 / (1 + rs)


def donchian_high(high: pd.Series, n: int) -> pd.Series:
    """唐奇安上轨：过去 n 日最高"""
    return high.rolling(n).max()


def donchian_low(low: pd.Series, n: int) -> pd.Series:
    """唐奇安下轨：过去 n 日最低"""
    return low.rolling(n).min()