"""LLM 提供者工厂：按配置创建实例；支持可扩展 backend registry。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from src.config.models import BusinessInterpretationConfig, MethodInterpretationConfig

from .ollama import OllamaProvider
from .protocol import LLMProvider


@dataclass(frozen=True)
class LLMProviderSelection:
    """LLM 提供者选择结果：可观测请求后端与实际后端。"""

    provider: LLMProvider
    requested_backend: str
    resolved_backend: str
    fallback_reason: str = ""


# (kwargs_without_fallback_flag, allow_fallback_to_ollama, requested_backend_lower) -> selection
LLMBackendBuilder = Callable[[dict[str, Any], bool, str], LLMProviderSelection]

_LLM_BACKEND_BUILDERS: dict[str, LLMBackendBuilder] = {}


def register_llm_backend(name: str, builder: LLMBackendBuilder) -> None:
    """注册自定义 LLM 后端，与 YAML ``llm_backend`` 字符串对齐（小写）。"""
    key = (name or "").strip().lower()
    if not key:
        raise ValueError("register_llm_backend: name 不能为空")
    _LLM_BACKEND_BUILDERS[key] = builder


def unregister_llm_backend(name: str) -> None:
    _LLM_BACKEND_BUILDERS.pop((name or "").strip().lower(), None)


def registered_llm_backend_names() -> tuple[str, ...]:
    return tuple(sorted(_LLM_BACKEND_BUILDERS.keys()))


def _make_ollama_selection(
    kwargs: dict[str, Any],
    requested: str,
    resolved: str,
    fallback_reason: str,
) -> LLMProviderSelection:
    return LLMProviderSelection(
        provider=OllamaProvider(
            base_url=kwargs.get("ollama_base_url") or kwargs.get("base_url", "http://127.0.0.1:11434"),
            model=kwargs.get("ollama_model") or kwargs.get("model", "qwen2.5:32b"),
            timeout=int(kwargs.get("timeout_seconds") or kwargs.get("timeout", 120)),
        ),
        requested_backend=requested,
        resolved_backend=resolved,
        fallback_reason=fallback_reason,
    )


def _build_openai_backend(kwargs: dict[str, Any], allow_fallback: bool, requested: str) -> LLMProviderSelection:
    try:
        from .openai_provider import OpenAIProvider

        return LLMProviderSelection(
            provider=OpenAIProvider.from_config_kwargs(kwargs),
            requested_backend=requested,
            resolved_backend="openai",
            fallback_reason="",
        )
    except ImportError as e:
        if not allow_fallback:
            raise RuntimeError(
                "llm_backend=openai 需要安装 openai 库：pip install openai"
                "（或在配置中设置 llm_allow_fallback_to_ollama: true 以回退本地 Ollama）"
            ) from e
        return _make_ollama_selection(
            kwargs,
            requested,
            "ollama",
            "openai 不可用：请 pip install openai（已回退 Ollama）",
        )


def _build_anthropic_backend(kwargs: dict[str, Any], allow_fallback: bool, requested: str) -> LLMProviderSelection:
    try:
        from .anthropic_provider import AnthropicProvider

        return LLMProviderSelection(
            provider=AnthropicProvider.from_config_kwargs(kwargs),
            requested_backend=requested,
            resolved_backend="anthropic",
            fallback_reason="",
        )
    except ImportError as e:
        if not allow_fallback:
            raise RuntimeError(
                "llm_backend=anthropic 需要安装 anthropic 库：pip install anthropic"
                "（或在配置中设置 llm_allow_fallback_to_ollama: true 以回退本地 Ollama）"
            ) from e
        return _make_ollama_selection(
            kwargs,
            requested,
            "ollama",
            "anthropic 不可用：请 pip install anthropic（已回退 Ollama）",
        )


def _install_default_llm_backends() -> None:
    if _LLM_BACKEND_BUILDERS:
        return
    register_llm_backend("openai", _build_openai_backend)
    register_llm_backend("anthropic", _build_anthropic_backend)


_install_default_llm_backends()


class LLMProviderFactory:
    """LLM 提供者工厂：根据 backend 创建 ollama、openai、anthropic 等实现。"""

    @staticmethod
    def interpretation_llm_kwargs_from_config(
        m: MethodInterpretationConfig | BusinessInterpretationConfig,
    ) -> dict[str, Any]:
        """从方法/业务解读配置对象提取 ``create_with_meta`` 所需 kwargs（含回退开关）。"""
        return {
            "ollama_base_url": m.ollama_base_url,
            "ollama_model": m.ollama_model,
            "timeout_seconds": m.timeout_seconds,
            "openai_api_key": m.openai_api_key,
            "openai_base_url": m.openai_base_url,
            "openai_model": m.openai_model,
            "openai_max_tokens": m.openai_max_tokens,
            "anthropic_api_key": m.anthropic_api_key,
            "anthropic_model": m.anthropic_model,
            "anthropic_max_tokens": m.anthropic_max_tokens,
            "llm_allow_fallback_to_ollama": m.llm_allow_fallback_to_ollama,
        }

    @staticmethod
    def from_method_interpretation(m: MethodInterpretationConfig) -> LLMProviderSelection:
        return LLMProviderFactory.create_with_meta(
            backend=m.llm_backend,
            **LLMProviderFactory.interpretation_llm_kwargs_from_config(m),
        )

    @staticmethod
    def from_business_interpretation(m: BusinessInterpretationConfig) -> LLMProviderSelection:
        return LLMProviderFactory.create_with_meta(
            backend=m.llm_backend,
            **LLMProviderFactory.interpretation_llm_kwargs_from_config(m),
        )

    @staticmethod
    def create(backend: str = "ollama", **kwargs: Any) -> LLMProvider:
        """仅返回 provider（兼容旧调用方）。"""
        return LLMProviderFactory.create_with_meta(backend=backend, **kwargs).provider

    @staticmethod
    def create_with_meta(backend: str = "ollama", **kwargs: Any) -> LLMProviderSelection:
        """
        创建 LLM 提供者实例，并返回可观测元信息。
        未在 registry 中注册的 backend 一律按 **ollama** 解析（与历史行为一致）。
        """
        cfg = dict(kwargs)
        allow_fallback = bool(cfg.pop("llm_allow_fallback_to_ollama", False))

        requested = (backend or "ollama").strip().lower()

        builder = _LLM_BACKEND_BUILDERS.get(requested)
        if builder is not None:
            return builder(cfg, allow_fallback, requested)

        return _make_ollama_selection(cfg, requested, "ollama", "")
