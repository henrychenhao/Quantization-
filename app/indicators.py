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


def kdj(high: pd.Series, low: pd.Series, close: pd.Series,
        n: int = 9, k_smooth: int = 3, d_smooth: int = 3):
    """KDJ 三线（国内行情软件口径）。返回 (k, d, j)。

    RSV = (C - LLV) / (HHV - LLV) × 100；K = SMA(RSV, 3, 1)、D = SMA(K, 3, 1)，
    ewm(alpha=1/3) 与通达信 SMA(X,N,M=1) 等价。
    """
    llv = low.rolling(n, min_periods=1).min()
    hhv = high.rolling(n, min_periods=1).max()
    rng = hhv - llv
    # 一字板（rng=0）时 RSV 无定义，取 50 中性（对齐行情软件口径）；保持 float dtype
    rsv = ((close - llv) / rng * 100).where(rng != 0, 50.0)
    k = rsv.ewm(alpha=1 / k_smooth, adjust=False).mean()
    d = k.ewm(alpha=1 / d_smooth, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    """ATR 真实波幅均值（Wilder 平滑，alpha=1/n）。"""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()