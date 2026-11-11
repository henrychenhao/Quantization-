"""全市场标的字典：A 股 + 场内 ETF，用于「添加自选」的名称/代码模糊搜索。

数据源（本机实测可用，东财接口被风控故不用）：
- akshare stock_info_a_code_name：沪深 A 股 5500+ 只（约 8 秒，分页拉取）
- akshare fund_etf_category_sina：新浪场内 ETF 1700+ 只（代码带 sh/sz 前缀）

本地缓存 data/stock_dict.csv；超过 7 天视为过期，后台线程自动刷新，
刷新失败沿用旧文件。首次无文件时 API 返回 ready=false，前端提示可直接手输代码。
"""
import os
import threading
import time

import pandas as pd

DICT_PATH = "data/stock_dict.csv"
_MAX_AGE = 7 * 86400          # 字典最长 7 天不刷新

_items_cache: list[dict] | None = None
_lock = threading.Lock()


def _market_of(code: str) -> str:
    """按代码前缀推断市场（仅用于展示）。"""
    if code.startswith(("51", "56", "58", "60", "68")):
        return "sh"
    if code.startswith(("15", "00", "30", "12")):
        return "sz"
    if code.startswith(("43", "83", "87", "92")):
        return "bj"
    return "sz"


def refresh(path: str | None = None) -> int:
    path = path or DICT_PATH
    """拉取全市场字典并覆盖保存，返回标的总数。"""
    import akshare as ak

    rows: list[dict] = []
    for _, r in ak.stock_info_a_code_name().iterrows():
        rows.append({"code": str(r["code"]), "name": str(r["name"]), "type": "stock"})
    etf = ak.fund_etf_category_sina(symbol="ETF基金")
    for _, r in etf.iterrows():
        code = str(r["代码"]).replace("sh", "").replace("sz", "")
        rows.append({"code": code, "name": str(r["名称"]), "type": "etf"})

    seen, uniq = set(), []
    for r in rows:
        if r["code"] in seen:
            continue
        seen.add(r["code"])
        r["market"] = _market_of(r["code"])
        uniq.append(r)

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    pd.DataFrame(uniq, columns=["code", "name", "type", "market"]).to_csv(path, index=False)
    return len(uniq)


def _load(path: str | None = None) -> list[dict]:
    path = path or DICT_PATH
    """加载字典到进程内缓存（文件存在时）。"""
    global _items_cache
    with _lock:
        if _items_cache is not None:
            return _items_cache
        if not os.path.exists(path):
            return []
        df = pd.read_csv(path, dtype={"code": str})
        _items_cache = df.to_dict("records")
        return _items_cache


def _reload(path: str | None = None) -> None:
    path = path or DICT_PATH
    global _items_cache
    with _lock:
        _items_cache = None
    _load(path)


def is_stale(path: str | None = None) -> bool:
    path = path or DICT_PATH
    """字典缺失或超过 7 天未刷新。"""
    if not os.path.exists(path):
        return True
    return time.time() - os.path.getmtime(path) > _MAX_AGE


def ensure_ready_async(path: str | None = None) -> None:
    path = path or DICT_PATH
    """后台线程确保字典就绪；文件新鲜时仅预热加载。不阻塞调用方。"""
    if not is_stale(path):
        _load(path)
        return

    def _job():
        try:
            refresh(path)
            print(f"[stock_dict] 字典刷新完成：{len(_load(path))} 只")
        except Exception as e:
            print(f"[stock_dict] 刷新失败（沿用旧文件）: {e}")
        _reload(path)

    threading.Thread(target=_job, daemon=True).start()


def search(q: str, limit: int = 20, path: str | None = None) -> tuple[list[dict], bool]:
    path = path or DICT_PATH
    """模糊搜索：代码前缀 > 名称包含 > 代码包含，返回 (items, ready)。

    ready=false 表示字典尚未就绪（首次拉取中），前端应提示可直接手输代码。
    """
    items = _load(path)
    if is_stale(path):
        ensure_ready_async(path)
    if not items:
        return [], False

    q = q.strip().lower()
    if not q:
        return [], True

    res: list[dict] = []
    seen: set[str] = set()

    def _add(it: dict) -> None:
        if it["code"] not in seen and len(res) < limit:
            seen.add(it["code"])
            res.append({**it, "name_l": None})

    for it in items:                       # 1) 代码前缀
        if str(it["code"]).lower().startswith(q):
            _add(it)
    for it in items:                       # 2) 名称包含
        if q in str(it["name"]).lower():
            _add(it)
    for it in items:                       # 3) 代码包含
        if q in str(it["code"]).lower():
            _add(it)

    for r in res:
        r.pop("name_l", None)
    return res, True