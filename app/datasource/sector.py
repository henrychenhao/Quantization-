"""同花顺行业指数源：AI 板块联动上下文。

数据链路（免费公开接口，无需 token）：
- 行业名单：stock_board_industry_name_ths（约 90 个行业）
- 行业指数日线：stock_board_industry_index_ths(symbol=行业名)

个股 → 行业的映射没有免费可靠接口（东财被风控、新浪成分接口已失效、
雪球要 token），采用「用户在自选页为个股选择行业」的轻配置方案，
行业名存 watchlist.industry，与同花顺行业名单对齐。
"""
import os
from datetime import datetime, timedelta

import pandas as pd

CACHE_DIR = os.path.join("data", "sector_index")


def get_industries() -> list[str]:
    """同花顺行业名单（约 90 个），失败返回空列表。"""
    try:
        import akshare as ak
        df = ak.stock_board_industry_name_ths()
        return sorted(df["name"].tolist())
    except Exception:
        return []


def _calc_perf(close: pd.Series) -> dict:
    """由收盘序列算近 1/5/20 日区间涨幅。"""

    def ret(n):
        return float(close.iloc[-1] / close.iloc[-n - 1] - 1) if len(close) > n else None

    return {"date": str(close.index[-1])[:10], "ret_1d": ret(1), "ret_5d": ret(5), "ret_20d": ret(20)}


def industry_perf(industry: str, days: int = 120) -> dict | None:
    """行业指数近期表现。日线缓存到 data/sector_index/，当日已拉过直接复用。

    返回 {date, ret_1d, ret_5d, ret_20d}；行业不存在或接口失败返回 None。
    """
    industry = (industry or "").strip()
    if not industry:
        return None
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{industry}.parquet")
    today = datetime.now().strftime("%Y-%m-%d")
    if os.path.exists(path):
        try:
            df = pd.read_parquet(path).dropna()
            if not df.empty and str(df.index[-1])[:10] >= today:
                return _calc_perf(df["close"])
        except Exception:
            pass
    try:
        import akshare as ak
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=90)).strftime("%Y%m%d")
        raw = ak.stock_board_industry_index_ths(symbol=industry, start_date=start, end_date=end)
        if raw is None or raw.empty:
            return None
        # 同花顺接口返回形态不稳定：有时 float64、有时带千分位逗号的字符串。
        # 统一按字符串清洗后转数字，剔除 NaN；有效样本不足视为失败（不缓存脏数据）。
        close = pd.to_numeric(
            raw["收盘价"].astype(str).str.replace(",", "").str.strip(),
            errors="coerce")
        # 注意：close 与日期都带原始 RangeIndex，用列构造再 set_index，
        # 避免 Series 对新 DatetimeIndex 对齐时产生全 NaN。
        df = pd.DataFrame({
            "date": pd.to_datetime(raw["日期"]),
            "close": close,
        }).dropna().set_index("date").sort_index()
        if len(df) < 25:
            return None
        df.to_parquet(path)
        return _calc_perf(df["close"])
    except Exception:
        return None
