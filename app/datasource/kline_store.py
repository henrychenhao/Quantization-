"""K 线本地库：Parquet 按代码分区，pandas 直读直写。

策略：拉一次存一份，日常增量；回测前若本地已覆盖目标区间则直接用，
否则从数据源重新拉取并覆盖更新。备份即拷目录。
"""
import os

import pandas as pd


def _path(code: str, kline_dir: str) -> str:
    return os.path.join(kline_dir, f"{code}.parquet")


def load_local(code: str, kline_dir: str) -> pd.DataFrame | None:
    """读取本地 K 线；不存在则返回 None。"""
    p = _path(code, kline_dir)
    if not os.path.exists(p):
        return None
    df = pd.read_parquet(p)
    df.index = pd.to_datetime(df.index)
    return df.sort_index()


def save(code: str, df: pd.DataFrame, kline_dir: str) -> str:
    """落盘（覆盖写）。返回文件路径。"""
    os.makedirs(kline_dir, exist_ok=True)
    p = _path(code, kline_dir)
    df.to_parquet(p)
    return p


def covers(df: pd.DataFrame, start: str | None, end: str | None) -> bool:
    """本地数据是否已覆盖 [start, end] 区间。"""
    if df is None or df.empty:
        return False
    lo, hi = df.index.min(), df.index.max()
    if start and lo > pd.Timestamp(start):
        return False
    if end and hi < pd.Timestamp(end):
        return False
    return True


def get_or_fetch(code: str, source, kline_dir: str,
                 start: str | None = None, end: str | None = None) -> pd.DataFrame:
    """优先复用本地 K 线；不足则拉取全量历史并保存，再按区间切片。"""
    local = load_local(code, kline_dir)
    if covers(local, start, end):
        df = local
    else:
        df = source.get_daily(code)          # 全量历史
        if not df.empty:
            save(code, df, kline_dir)
    if start:
        df = df[df.index >= start]
    if end:
        df = df[df.index <= end]
    return df


def refresh(code: str, source, kline_dir: str) -> int:
    """强制全量刷新某标的 K 线（每日收盘数据同步用）。返回根数。"""
    df = source.get_daily(code)
    if not df.empty:
        save(code, df, kline_dir)
    return len(df)