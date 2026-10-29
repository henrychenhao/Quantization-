"""LLM 客户端：调用 DeepSeek（OpenAI 兼容接口），支持流式输出。

密钥来自 config.yaml 的 llm 段，不经手任何第三方；api_key 为空时
抛 LLMNotConfigured，上层转成对用户友好的提示。
"""
import json
from typing import Iterator

import requests


class LLMNotConfigured(Exception):
    """未配置 API Key。"""


class LLMClient:
    def __init__(self, cfg: dict):
        self.base_url = (cfg.get("base_url") or "https://api.deepseek.com").rstrip("/")
        self.api_key = (cfg.get("api_key") or "").strip()
        self.model = cfg.get("model") or "deepseek-chat"
        self.temperature = float(cfg.get("temperature", 0.3))
        if not self.api_key:
            raise LLMNotConfigured("未配置 API Key（config.yaml → llm.api_key）")

    def stream_chat(self, messages: list[dict]) -> Iterator[str]:
        """流式对话：逐块 yield 助手回复文本片段。

        网络或接口异常时抛出，由上层捕获后以错误事件返回前端。
        """
        resp = requests.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            json={
                "model": self.model,
                "messages": messages,
                "temperature": self.temperature,
                "stream": True,
            },
            timeout=180,
            stream=True,
        )
        if resp.status_code != 200:
            detail = ""
            try:
                detail = resp.json().get("error", {}).get("message", "")
            except Exception:
                detail = resp.text[:200]
            raise RuntimeError(f"模型接口返回 {resp.status_code}：{detail}")

        for raw in resp.iter_lines(decode_unicode=True):
            if not raw or not raw.startswith("data:"):
                continue
            payload = raw[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            delta = chunk.get("choices", [{}])[0].get("delta", {})
            text = delta.get("content")
            if text:
                yield text
