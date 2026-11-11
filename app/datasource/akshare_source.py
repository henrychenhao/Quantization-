"""AkShare 数据源实现（P0 主源：新浪；备源：东财）。

注意：当前网络环境下东财接口（requests）会被风控断开（RemoteDisconnected），
新浪接口稳定可用，故 P0 以新浪为主源；东财保留为备源，换网络/环境后可用。
所有行情库调用都收敛在本文件，上层从不 import akshare。

指数：代码带市场前缀（sh000001 上证 / sz399001 深成 / sz399006 创业板…），
K 线与实时均走新浪指数接口，与个股/ETF 共用同一 K 线存储（文件名天然不冲突）。
"""
import re

import pandas as pd

from .base import DataSource, is_etf

# 统一 K 线 schema
KLINE_COLS = ["open", "high", "low", "close", "volume", "amount"]

# 系统支持的指数（大盘总览用）
INDEX_CODES = {"sh000001": "上证指数", "sz399001": "深证成指", "sz399006": "创业板指"}


def is_index(code: str) -> bool:
    """是否为带市场前缀的指数代码（sh000xxx / sz399xxx）。"""
    return bool(re.match(r"^(sh|sz)(000|399)\d{3}$", code or ""))


def sina_symbol(code: str) -> str:
    """新浪接口需要市场前缀：沪市 sh / 深市 sz；已带前缀的代码（指数）原样透传。"""
    if code[:2] in ("sh", "sz", "bj"):
        return code
    return "sh" + code if code.startswith(("5", "6", "9")) else "sz" + code


def _standardize(df: pd.DataFrame) -> pd.DataFrame:
    """新浪返回已是英文列名，统一为 schema 并按日期索引；缺 amount 列补 0（指数）。"""
    df = df.copy()
    df.index = pd.to_datetime(df["date"])
    if "amount" not in df.columns:
        df["amount"] = 0.0
    df = df[KLINE_COLS].apply(pd.to_numeric, errors="coerce").dropna()
    df = df.sort_index()
    return df


class AkShareSource(DataSource):
    """主源：新浪。ETF 与个股的历史日线均为前复权。"""

    def get_daily(self, code: str, start: str | None = None, end: str | None = None) -> pd.DataFrame:
        import akshare as ak

        sym = sina_symbol(code)
        if is_index(code):
            raw = ak.stock_zh_index_daily(symbol=sym)         # 新浪指数日线（未复权点位）
        elif is_etf(code):
            raw = ak.fund_etf_hist_sina(symbol=sym)           # 新浪场内 ETF 日线
        else:
            raw = ak.stock_zh_a_daily(symbol=sym, adjust="qfq")  # 新浪个股前复权

        df = _standardize(raw)
        if start:
            df = df[df.index >= start]
        if end:
            df = df[df.index <= end]
        return df

    def get_spot(self, code: str) -> float:
        """新浪单只实时价（hq.sinajs.cn，直连，秒级返回）。

        盘中轮询专用；返回 0 表示无有效行情（停牌/未上市/接口异常）。
        """
        import requests
        try:
            url = f"https://hq.sinajs.cn/list={sina_symbol(code)}"
            headers = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}
            r = requests.get(url, headers=headers, timeout=8)
            r.encoding = "gbk"
            parts = r.text.split('"')
            if len(parts) >= 2:
                fields = parts[1].split(",")
                if len(fields) > 3 and fields[3]:
                    return float(fields[3])
        except Exception:
            pass
        return 0.0

    def get_spots(self, codes: list[str]) -> dict[str, dict]:
        """批量实时行情（新浪 hq.sinajs.cn 一次拉多只）。

        返回 {code: {"name": 名称, "price": 最新价, "prev_close": 昨收, "change_pct": 涨跌幅%}}；
        无有效行情的标的不出现。
        """
        if not codes:
            return {}
        import requests
        syms = ",".join(sina_symbol(c) for c in codes)
        try:
            r = requests.get(f"https://hq.sinajs.cn/list={syms}",
                             headers={"Referer": "https://finance.sina.com.cn",
                                      "User-Agent": "Mozilla/5.0"}, timeout=8)
            r.encoding = "gbk"
        except Exception:
            return {}
        out = {}
        for line in r.text.strip().splitlines():
            head, _, rest = line.partition('="')
            sym = head.rsplit("_", 1)[-1].strip()          # sh600519 / sh000001
            code = sym[2:] if sym[:2] in ("sh", "sz", "bj") else sym
            # 请求可能是纯代码（个股/ETF）或带前缀（指数 sh000001）：两种 key 都匹配，
            # 返回时保持请求方传入的原样代码
            if code not in codes and sym not in codes:
                continue
            key = sym if sym in codes else code
            f = rest.strip().strip('";').split(",")
            if len(f) < 4 or not f[3]:
                continue
            try:
                price = float(f[3]); prev = float(f[2]) if f[2] else price
            except ValueError:
                continue
            out[key] = {
                "name": f[0], "price": price, "prev_close": prev,
                "change_pct": (price / prev - 1) * 100 if prev else 0.0,
            }
        return out


class EastMoneySource(DataSource):
    """备源：东财（当前网络可能被风控断开，供后续换网络后启用）。"""

    def get_daily(self, code: str, start: str | None = None, end: str | None = None) -> pd.DataFrame:
        import akshare as ak

        if is_etf(code):
            raw = ak.fund_etf_hist_em(symbol=code, period="daily", adjust="qfq")
        else:
            raw = ak.stock_zh_a_hist(
                symbol=code, period="daily",
                start_date=(start or "19900101").replace("-", ""),
                end_date=(end or "20991231").replace("-", ""),
                adjust="qfq",
            )
        df = raw.rename(columns={
            "日期": "date", "开盘": "open", "最高": "high",
            "最低": "low", "收盘": "close", "成交量": "volume", "成交额": "amount",
        })
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")[KLINE_COLS].apply(pd.to_numeric, errors="coerce").dropna()
        if start:
            df = df[df.index >= start]
        if end:
            df = df[df.index <= end]
        return df

    def get_spot(self, code: str) -> float:
        raise NotImplementedError("实时快照接入安排在 P1")