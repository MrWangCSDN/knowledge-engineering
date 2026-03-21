"""OpenAI 及兼容 API（Azure、转发网关、vLLM 等）的 LLM 提供者。"""
from __future__ import annotations

import os
from typing import Any, Optional


class OpenAIProvider:
    """
    使用官方 ``openai`` SDK 的 ``chat.completions``，与 Ollama 侧 ``generate`` 签名对齐。

    - ``base_url`` 为空时使用官方 ``api.openai.com``；非空时可指向兼容服务。
    - ``api_key`` 优先参数，否则读环境变量 ``OPENAI_API_KEY``。
    """

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: str = "gpt-4o-mini",
        timeout_seconds: int = 120,
        max_tokens: Optional[int] = None,
    ):
        try:
            from openai import OpenAI
        except ImportError as e:
            raise ImportError(
                "使用 llm_backend: openai 需要安装 openai 库：pip install openai"
            ) from e

        key = (api_key or "").strip() or (os.environ.get("OPENAI_API_KEY") or "").strip()
        if not key:
            raise ValueError(
                "OpenAI：未配置 api_key，请在 YAML 中设置 openai_api_key 或设置环境变量 OPENAI_API_KEY"
            )

        kwargs: dict[str, Any] = {"api_key": key}
        if base_url and str(base_url).strip():
            kwargs["base_url"] = str(base_url).rstrip("/")
        self._client = OpenAI(**kwargs)
        self._model = model or "gpt-4o-mini"
        self._timeout = float(timeout_seconds)
        self._max_tokens = max_tokens

    @classmethod
    def from_config_kwargs(cls, raw: dict[str, Any]) -> OpenAIProvider:
        """从 method_interpretation / business_interpretation 的配置 dict 构造。"""
        return cls(
            api_key=raw.get("openai_api_key"),
            base_url=raw.get("openai_base_url"),
            model=str(raw.get("openai_model") or "gpt-4o-mini"),
            timeout_seconds=int(raw.get("timeout_seconds") or 120),
            max_tokens=raw.get("openai_max_tokens"),
        )

    def generate(self, prompt: str, **kwargs: Any) -> str:
        model = kwargs.get("model") or self._model
        timeout = float(kwargs.get("timeout", self._timeout))
        max_tokens = kwargs.get("max_tokens", self._max_tokens)
        create_kwargs: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "timeout": timeout,
        }
        if max_tokens is not None:
            create_kwargs["max_tokens"] = int(max_tokens)

        resp = self._client.chat.completions.create(**create_kwargs)
        if not resp.choices:
            return ""
        msg = resp.choices[0].message
        return (msg.content or "").strip()
