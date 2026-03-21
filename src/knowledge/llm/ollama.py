"""Ollama LLM 提供者。"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class OllamaProvider:
    """Ollama 本地模型提供者。"""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "qwen2.5:32b",
        timeout: int = 120,
    ):
        self.base_url = (base_url or "http://127.0.0.1:11434").rstrip("/")
        self.model = model or "qwen2.5:32b"
        self.timeout = timeout

    def generate(self, prompt: str, **kwargs: Any) -> str:
        """调用 Ollama /api/generate 生成文本。"""
        model = kwargs.get("model") or self.model
        timeout = int(kwargs.get("timeout", self.timeout))
        url = f"{self.base_url}/api/generate"
        body = json.dumps(
            {"model": model, "prompt": prompt, "stream": False},
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8", errors="replace"))
        return (raw.get("response") or "").strip()
