"""全市场行业映射测试：翻页抓取 / 缓存复用 / 识别查询（网络全部打桩）。"""
import json

from types import SimpleNamespace

from app.datasource import industry_map


def _resp(html):
    r = SimpleNamespace()
    r.status_code = 200
    r.text = html
    return r


def _page(codes):
    """模拟同花顺行业详情页：每个代码渲染为一个 stockpage 链接。"""
    cells = "".join(
        f'<td><a target="_blank" href="https://stockpage.10jqka.com.cn/{c}/">{c}</a></td>'
        for c in codes)
    return f'<table><tbody><tr>{cells}</tr></tbody></table>'


# ---------- 单行业翻页 ----------

def test_fetch_industry_codes_paginates(monkeypatch):
    """第一页满 20 只继续翻、不足 20 只停页。"""
    calls = []

    def fake_get(url, **k):
        calls.append(url)
        return _resp(_page([f"{600000 + i:06d}" for i in range(20)]) if len(calls) == 1
                     else _page(["000001", "000002", "000003", "000004", "000005"]))

    monkeypatch.setattr(industry_map.requests, "get", fake_get)
    codes = industry_map._fetch_industry_codes("881121")
    assert len(codes) == 25
    assert len(calls) == 2
    assert calls[1].endswith("/page/2/")


def test_fetch_stops_on_error_page(monkeypatch):
    """空页（无代码）立即停。"""
    def fake_get(url, **k):
        return _resp("<html>无数据</html>")
    monkeypatch.setattr(industry_map.requests, "get", fake_get)
    assert industry_map._fetch_industry_codes("881121") == []


# ---------- 全量构建与缓存 ----------

def test_build_map_and_reuse_cache(tmp_path, monkeypatch):
    """构建写缓存；再次调用（非 force）直接复用不再发请求。"""
    path = str(tmp_path / "industry_map.json")
    monkeypatch.setattr(industry_map, "MAP_PATH", path)
    monkeypatch.setattr(industry_map, "_cache", None)
    monkeypatch.setattr(industry_map, "_industry_codes", lambda: {"881121": "半导体", "881169": "证券"})
    calls = {"n": 0}

    def fake_get(url, **k):
        calls["n"] += 1
        # 两个行业用不同代码段：半导体 60xxxx、证券 00xxxx
        codes = ([f"{600000 + i:06d}" for i in range(5)]
                 if "881121" in url else [f"{i:06d}" for i in range(1, 6)])
        return _resp(_page(codes))

    monkeypatch.setattr(industry_map.requests, "get", fake_get)
    m = industry_map.build_map(force=True)
    assert len(m) == 10                        # 两个行业各 5 只
    assert m["600000"] == "半导体"
    assert json.load(open(path))["600000"] == "半导体"

    n_before = calls["n"]
    m2 = industry_map.build_map()              # 走缓存
    assert m2 == m
    assert calls["n"] == n_before              # 没有新请求


def test_build_map_failure_keeps_no_cache(tmp_path, monkeypatch):
    """行业名单失败 → 返回空且不写缓存文件。"""
    path = str(tmp_path / "industry_map.json")
    monkeypatch.setattr(industry_map, "MAP_PATH", path)
    monkeypatch.setattr(industry_map, "_cache", None)
    monkeypatch.setattr(industry_map, "_industry_codes", lambda: {})
    assert industry_map.build_map(force=True) == {}
    assert not (tmp_path / "industry_map.json").exists()


def test_detect_industry_reads_cache(tmp_path, monkeypatch):
    path = str(tmp_path / "industry_map.json")
    monkeypatch.setattr(industry_map, "MAP_PATH", path)
    monkeypatch.setattr(industry_map, "_cache", None)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"002156": "半导体"}, f)
    assert industry_map.detect_industry("002156") == "半导体"
    assert industry_map.detect_industry("300607") is None
    assert industry_map.detect_industry("") is None
    st = industry_map.map_status()
    assert st == {"stocks": 1, "built": True}
