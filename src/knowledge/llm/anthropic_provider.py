"""Anthropic Messages API 的 LLM 提供者。"""
from __future__ import annotations

import os
from typing import Any, Optional


class AnthropicProvider:
    """
    使用 ``anthropic`` SDK。api_key 优先参数，否则读环境变量 ``ANTHROPIC_API_KEY``。
    """

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        model: str = "claude-3-5-sonnet-20241022",
        timeout_seconds: int = 120,
        max_tokens: int = 8192,
    ):
        try:
            import anthropic
        except ImportError as e:
            raise ImportError(
                "使用 llm_backend: anthropic 需要安装 anthropic 库：pip install anthropic"
            ) from e

        key = (api_key or "").strip() or (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        if not key:
            raise ValueError(
                "Anthropic：未配置 api_key，请在 YAML 中设置 anthropic_api_key 或设置环境变量 ANTHROPIC_API_KEY"
            )

        self._client = anthropic.Anthropic(api_key=key)
        self._model = model or "claude-3-5-sonnet-20241022"
        self._timeout = float(timeout_seconds)
        self._max_tokens = int(max_tokens)

    @classmethod
    def from_config_kwargs(cls, raw: dict[str, Any]) -> AnthropicProvider:
        return cls(
            api_key=raw.get("anthropic_api_key"),
            model=str(
                raw.get("anthropic_model") or "claude-3-5-sonnet-20241022"
            ),
            timeout_seconds=int(raw.get("timeout_seconds") or 120),
            max_tokens=int(raw.get("anthropic_max_tokens") or 8192),
        )

    def generate(self, prompt: str, **kwargs: Any) -> str:
        model = kwargs.get("model") or self._model
        timeout = float(kwargs.get("timeout", self._timeout))
        max_tokens = int(kwargs.get("max_tokens", self._max_tokens))

        msg = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
            timeout=timeout,
        )
        parts: list[str] = []
        for block in msg.content:
            if hasattr(block, "text") and block.text:
                parts.append(block.text)
        return "".join(parts).strip()
