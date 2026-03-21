"""LLM 提供者：策略模式，支持 Ollama、OpenAI、Anthropic 等。"""
from .protocol import LLMProvider
from .ollama import OllamaProvider
from .factory import (
    LLMBackendBuilder,
    LLMProviderFactory,
    LLMProviderSelection,
    register_llm_backend,
    registered_llm_backend_names,
    unregister_llm_backend,
)

__all__ = [
    "LLMProvider",
    "OllamaProvider",
    "LLMProviderFactory",
    "LLMProviderSelection",
    "LLMBackendBuilder",
    "register_llm_backend",
    "unregister_llm_backend",
    "registered_llm_backend_names",
]
