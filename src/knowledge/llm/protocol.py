"""LLM 提供者抽象接口。"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LLMProvider(Protocol):
    """LLM 提供者接口：生成文本。"""

    def generate(self, prompt: str, **kwargs: Any) -> str:
        """
        根据 prompt 生成文本。
        :param prompt: 输入提示
        :param kwargs: 可选参数（如 timeout、max_tokens 等）
        :return: 生成的文本
        """
        ...
