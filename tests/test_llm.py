"""AI 分析模块测试：上下文构建器 + LLM 客户端（全部离线，不调用真实接口）。"""
import json

import pytest

from app import db
from app.account import core as account
from app.llm import analyst
from app.llm.client import LLMClient, LLMNotConfigured
from conftest import make_df

CFG = {"data": {"kline_dir": "data/kline"}}


@pytest.fixture
def stub_kline(monkeypatch):
    """构造 60 根先跌后涨的行情，避开真实网络。"""
    closes = [50.0 - i * 0.2 for i in range(30)] + [44.0 + i * 0.3 for i in range(30)]
    monkeypatch.setattr(analyst, "get_or_fetch", lambda *a: make_df(closes))
    return closes


# ---------- 个股诊断上下文 ----------

def test_stock_context_contains_key_sections(stub_kline):
    db.add_watchlist("600519", "贵州茅台", ["double_ma"])
    text = analyst.stock_context("600519", CFG)
    for key in ("【标的】", "【行情】", "【均线】", "【指标】", "【通道】", "【策略状态】", "【持仓】"):
        assert key in text, f"缺少 {key}"
    assert "600519" in text and "贵州茅台" in text


def test_stock_context_marks_etf_rules(stub_kline):
    db.add_watchlist("513530", "红利ETF", ["double_ma"])
    text = analyst.stock_context("513530", CFG)
    assert "ETF" in text and "T+0" in text


def test_stock_context_insufficient_data(monkeypatch):
    monkeypatch.setattr(analyst, "get_or_fetch", lambda *a: make_df([10.0] * 5))
    text = analyst.stock_context("600519", CFG)
    assert "数据不足" in text


# ---------- 持仓复盘上下文 ----------

def test_portfolio_context_empty():
    text = analyst.portfolio_context()
    assert "当前空仓" in text


def test_portfolio_context_with_position(seeded_account, monkeypatch):
    db.add_trade(None, "600519", "buy", 10.0, 1000, 5.0)
    # 最新价：打桩本地 K 线（收盘 11.0 → 浮盈 +10%）
    import app.datasource.kline_store as ks
    monkeypatch.setattr(ks, "load_local", lambda *a: make_df([11.0] * 30))
    text = analyst.portfolio_context()
    assert "600519" in text
    assert "1000 股" in text
    assert "浮动盈亏 +9.95%" in text   # 成本 10.005（含费）→ 11/10.005-1 ≈ +9.95%


# ---------- 信号解读上下文 ----------

def test_signal_context_pending(seeded_account):
    db.add_signal("600519", "贵州茅台", "buy", "grid", "网格买入档触发（现价 10.00 跌破锚点 -3%）", 10.0, 800)
    text = analyst.signal_context()
    assert "【待处理信号】" in text
    assert "800 股" in text
    assert "grid" in text


def test_signal_context_empty():
    text = analyst.signal_context()
    assert "没有待处理信号" in text


# ---------- 策略问答 / 系统提示词 ----------

def test_strategy_knowledge_covers_all_six():
    text = analyst.strategy_knowledge()
    for name in ("双均线交叉", "MACD", "布林带", "唐奇安", "RSI", "网格交易"):
        assert name in text


def test_system_prompt_without_scope():
    prompt = analyst.system_prompt(None, CFG)
    assert "量化分析助手" in prompt
    assert "不要编造" in prompt
    assert "【账户概况】" in prompt


def test_system_prompt_with_scope(stub_kline):
    db.add_watchlist("600519", "贵州茅台", ["double_ma"])
    prompt = analyst.system_prompt("600519", CFG)
    assert "【标的】600519" in prompt


def test_system_prompt_scope_error_handled(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("行情接口超时")
    monkeypatch.setattr(analyst, "get_or_fetch", boom)
    prompt = analyst.system_prompt("600519", CFG)
    assert "行情数据获取失败" in prompt


# ---------- LLM 客户端 ----------

def test_client_not_configured_raises():
    with pytest.raises(LLMNotConfigured):
        LLMClient({})


def test_client_stream_parses_sse(monkeypatch):
    class FakeResp:
        status_code = 200
        @staticmethod
        def iter_lines(decode_unicode=True):
            yield 'data: {"choices":[{"delta":{"content":"你好"}}]}'
            yield 'data: {"choices":[{"delta":{"content":"，世界"}}]}'
            yield "data: [DONE]"

    captured = {}
    def fake_post(url, **kw):
        captured["url"] = url
        captured["json"] = kw["json"]
        return FakeResp()

    monkeypatch.setattr("app.llm.client.requests.post", fake_post)
    client = LLMClient({"base_url": "https://api.deepseek.com", "api_key": "sk-test", "model": "deepseek-chat"})
    chunks = list(client.stream_chat([{"role": "user", "content": "hi"}]))
    assert chunks == ["你好", "，世界"]
    assert captured["url"].endswith("/chat/completions")
    assert captured["json"]["stream"] is True
    assert captured["json"]["messages"][0]["role"] == "user"


def test_client_http_error_raises(monkeypatch):
    class FakeResp:
        status_code = 401
        text = '{"error":{"message":"Authentication Fails"}}'
        def json(self):
            return {"error": {"message": "Authentication Fails"}}
    monkeypatch.setattr("app.llm.client.requests.post", lambda *a, **k: FakeResp())
    client = LLMClient({"api_key": "sk-bad"})
    with pytest.raises(RuntimeError, match="401"):
        list(client.stream_chat([{"role": "user", "content": "hi"}]))