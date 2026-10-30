"""腾讯实时行情源：单次批量请求拿全「个股基本面 + 五档盘口」。

为什么用腾讯：东财接口本机被风控（RemoteDisconnected）、雪球需要登录
token；腾讯 qt.gtimg.cn 直连稳定、无需认证，88 字段涵盖 PE(TTM)/PB/
总市值/换手率/量比与五档买卖挂单，一次请求最多约 60 只。

定位：仅用于 AI 分析上下文（基本面/盘口），不参与信号引擎——
信号引擎的实时价走新浪 get_spots，互不干扰。
"""
import requests

_URL = "https://qt.gtimg.cn/q="
_HEADERS = {"Referer": "https://gu.qq.com/"}
_CHUNK = 50


def _prefix(code: str) -> str:
    """腾讯代码带市场前缀：沪 sh / 深 sz。"""
    return ("sh" if code.startswith(("6", "5", "9")) else "sz") + code


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def get_quotes(codes) -> dict[str, dict]:
    """批量实时快照。返回 {code: {price, pe, pb, mktcap, ..., bid_vol, ask_vol}}。

    容错：网络失败/单只解析失败仅跳过该批或该只，不抛异常。
    五档挂单量为买一~买五/卖一~卖五合计（单位：手）。
    """
    out: dict[str, dict] = {}
    codes = [c for c in codes if c]
    for i in range(0, len(codes), _CHUNK):
        chunk = codes[i:i + _CHUNK]
        try:
            r = requests.get(_URL + ",".join(_prefix(c) for c in chunk),
                             headers=_HEADERS, timeout=8)
            r.encoding = "gbk"
        except Exception:
            continue
        for line in r.text.strip().split(";"):
            parts = line.strip().split("~")
            if len(parts) < 50 or not parts[2]:
                continue
            try:
                out[parts[2]] = {
                    "name": parts[1],
                    "price": _f(parts[3]),
                    "prev_close": _f(parts[4]),
                    "bid_vol": sum(_f(parts[k]) for k in (10, 12, 14, 16, 18)),
                    "ask_vol": sum(_f(parts[k]) for k in (20, 22, 24, 26, 28)),
                    "turnover": _f(parts[38]),      # 换手率 %
                    "pe": _f(parts[39]),             # PE(TTM)
                    "pb": _f(parts[46]),             # 市净率
                    "mktcap": _f(parts[45]),         # 总市值（亿）
                    "float_mktcap": _f(parts[44]),   # 流通市值（亿）
                    "vol_ratio": _f(parts[49]),       # 量比
                }
            except (ValueError, IndexError):
                continue
    return out
