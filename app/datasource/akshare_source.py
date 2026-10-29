"""AkShare 数据源实现（P0 主源：新浪；备源：东财）。

注意：当前网络环境下东财接口（requests）会被风控断开（RemoteDisconnected），
新浪接口稳定可用，故 P0 以新浪为主源；东财保留为备源，换网络/环境后可用。
所有行情库调用都收敛在本文件，上层从不 import akshare。
"""
import pandas as pd

from .base import DataSource, is_etf

# 统一 K 线 schema
KLINE_COLS = ["open", "high", "low", "close", "volume", "amount"]


def sina_symbol(code: str) -> str:
    """新浪接口需要市场前缀：沪市 sh / 深市 sz。"""
    return "sh" + code if code.startswith(("5", "6", "9")) else "sz" + code


def _standardize(df: pd.DataFrame) -> pd.DataFrame:
    """新浪返回已是英文列名，统一为 schema 并按日期索引。"""
    df = df.copy()
    df.index = pd.to_datetime(df["date"])
    df = df[KLINE_COLS].apply(pd.to_numeric, errors="coerce").dropna()
    df = df.sort_index()
    return df


class AkShareSource(DataSource):
    """主源：新浪。ETF 与个股的历史日线均为前复权。"""

    def get_daily(self, code: str, start: str | None = None, end: str | None = None) -> pd.DataFrame:
        import akshare as ak

        sym = sina_symbol(code)
        if is_etf(code):
            raw = ak.fund_etf_hist_sina(symbol=sym)          # 新浪场内 ETF 日线
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
            sym = head.rsplit("_", 1)[-1].strip()          # sh600519
            code = sym[2:] if sym[:2] in ("sh", "sz", "bj") else sym
            if code not in codes:
                continue
            f = rest.strip().strip('";').split(",")
            if len(f) < 4 or not f[3]:
                continue
            try:
                price = float(f[3]); prev = float(f[2]) if f[2] else price
            except ValueError:
                continue
            out[code] = {
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