"""全市场「股票代码 → 同花顺行业名」映射。

数据链路（与行业指数同源同体系，识别结果可直接用于板块联动）：
  1. 行业名单与代码：akshare stock_board_industry_name_ths（90 个行业）
  2. 各行业成分股：同花顺行业详情页翻页抓取（每页 20 只）
     https://q.10jqka.com.cn/thshy/detail/code/{行业代码}/[page/{n}/]

构建约 280~320 次请求（5000+ 股 ÷ 20/页），4 线程并发约 40~60 秒，
一次构建缓存到 data/industry_map.json 长期复用；建议随季度或板块调整
手动重建（force=True）。

定位：仅为 AI 板块联动提供「个股 → 行业」的自动识别，不参与信号引擎。
"""
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

MAP_PATH = os.path.join("data", "industry_map.json")
_DETAIL = "https://q.10jqka.com.cn/thshy/detail/code/{code}/"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
}
_PAGE_SIZE = 20
_MAX_PAGES = 30            # 单行业翻页上限（>600 只的行业不存在，防异常死循环）
_WORKERS = 4

_lock = threading.Lock()
_cache: dict | None = None


def _load() -> dict:
    global _cache
    with _lock:
        if _cache is None:
            try:
                with open(MAP_PATH, encoding="utf-8") as f:
                    _cache = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                _cache = {}
        return _cache


def _industry_codes() -> dict[str, str]:
    """{行业代码: 行业名}（约 90 个），失败返回空。"""
    try:
        import akshare as ak
        df = ak.stock_board_industry_name_ths()
        return dict(zip(df["code"].astype(str), df["name"]))
    except Exception:
        return {}


def _fetch_industry_codes(industry_code: str) -> list[str]:
    """翻页抓单个行业的全部成分股代码（每页 20 只，翻到空页停）。"""
    url = _DETAIL.format(code=industry_code)
    out: list[str] = []
    for page in range(1, _MAX_PAGES + 1):
        page_url = url if page == 1 else f"{url}page/{page}/"
        try:
            r = requests.get(page_url, headers=_HEADERS, timeout=10)
            r.encoding = "gbk"
            codes = re.findall(r"stockpage\.10jqka\.com\.cn/(\d{6})/", r.text)
        except Exception:
            break
        if not codes:
            break
        out.extend(codes)
        if len(codes) < _PAGE_SIZE:
            break
        time.sleep(0.15)     # 温和限速，降低被反爬概率
    return list(dict.fromkeys(out))


def build_map(force: bool = False) -> dict:
    """构建全量映射并缓存。已有缓存且非 force 时直接复用。

    返回 {code: 行业名}；行业名单失败（网络/接口异常）返回空 dict，
    调用方应提示用户稍后重试，不写空缓存。
    """
    if not force:
        cached = _load()
        if cached:
            return cached
    industries = _industry_codes()
    if not industries:
        return {}
    mapping: dict[str, str] = {}

    def work(item):
        icode, iname = item
        codes = _fetch_industry_codes(icode)
        return iname, codes

    with ThreadPoolExecutor(max_workers=_WORKERS) as ex:
        futures = {ex.submit(work, it): it for it in industries.items()}
        for fu in as_completed(futures):
            try:
                iname, codes = fu.result()
                for c in codes:
                    mapping[c] = iname
            except Exception:
                continue
    if not mapping:
        return {}
    os.makedirs(os.path.dirname(MAP_PATH), exist_ok=True)
    tmp = MAP_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False)
    os.replace(tmp, MAP_PATH)
    global _cache
    with _lock:
        _cache = dict(mapping)
    return mapping


def detect_industry(code: str) -> str | None:
    """识别个股所属同花顺行业；缓存未命中（如新股/未构建）返回 None。"""
    code = (code or "").strip()
    if not code.isdigit():
        return None
    return _load().get(code)


def map_status() -> dict:
    """映射缓存概况，供前端提示是否需要先构建。"""
    m = _load()
    return {"stocks": len(m), "built": bool(m)}
