"""全市场标的字典搜索：代码前缀 > 名称包含 > 代码包含（临时字典文件，不触网）。"""
import pandas as pd
import pytest

from app.datasource import stock_dict


@pytest.fixture(autouse=True)
def temp_dict(tmp_path, monkeypatch):
    """每个用例用独立临时字典文件，测试完恢复全局缓存。"""
    p = tmp_path / "stock_dict.csv"
    pd.DataFrame([
        {"code": "600519", "name": "贵州茅台", "type": "stock", "market": "sh"},
        {"code": "000977", "name": "浪潮信息", "type": "stock", "market": "sz"},
        {"code": "513530", "name": "港股通红利ETF华泰柏瑞", "type": "etf", "market": "sh"},
        {"code": "300058", "name": "蓝色光标", "type": "stock", "market": "sz"},
        {"code": "601179", "name": "中国西电", "type": "stock", "market": "sh"},
    ]).to_csv(p, index=False)
    monkeypatch.setattr(stock_dict, "DICT_PATH", str(p))
    monkeypatch.setattr(stock_dict, "_items_cache", None)
    yield
    stock_dict._items_cache = None


def test_search_by_code_prefix():
    items, ready = stock_dict.search("6005")
    assert ready is True
    assert [i["code"] for i in items] == ["600519"]


def test_search_by_name_contains():
    items, _ = stock_dict.search("茅台")
    assert items[0]["code"] == "600519"


def test_search_etf_by_name():
    items, _ = stock_dict.search("红利")
    assert "513530" in [i["code"] for i in items]


def test_search_prefix_priority_and_code_order():
    """代码前缀命中优先，且同轮按代码顺序排列。"""
    items, _ = stock_dict.search("60")
    codes = [i["code"] for i in items]
    assert set(codes) == {"600519", "601179"}
    assert codes.index("600519") < codes.index("601179")


def test_search_no_match_returns_empty():
    items, ready = stock_dict.search("不存在的标的xyz")
    assert items == [] and ready is True


def test_search_blank_query_returns_empty():
    items, _ = stock_dict.search("   ")
    assert items == []


def test_search_dict_missing_not_ready(tmp_path, monkeypatch):
    """字典未就绪时返回 ready=False，且不触发真实网络拉取（refresh 打桩）。"""
    monkeypatch.setattr(stock_dict, "DICT_PATH", str(tmp_path / "none.csv"))
    monkeypatch.setattr(stock_dict, "_items_cache", None)
    monkeypatch.setattr(stock_dict, "refresh", lambda *a, **k: 0)   # 拦截后台刷新
    items, ready = stock_dict.search("茅台")
    assert items == [] and ready is False
    stock_dict._items_cache = None