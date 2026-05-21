"""LLM provider 工厂：按用户选的 model 名实例化对应 provider。

核心职责：
  - 把"用户友好的模型选择"（如 'qwen-plus', 'MiniMax-M2'）映射到底层 provider 类
  - 缓存已实例化的 provider（避免每次请求都重建 httpx client）
  - 兜底：未知 / 空 / 配置失败 → 回退到默认 provider

设计：
  - 当前支持两个 catalog 项：qwen-plus（DashScope）+ MiniMax-M2（MiniMax）
  - 默认 model = SUPPORTED_MODELS[0]（即 qwen-plus）；可通过 KE_DEFAULT_MODEL env 覆盖
  - 新增 provider 时只需在 SUPPORTED_MODELS 增条目 + _build_provider 加分支

未来扩展（YAGNI 暂不做）：
  - per-user / per-org 配额管理（在 factory 层加 hook）
  - 模型能力差异化（如某模型不支持 tool_calls 时降级）
"""
# from __future__ import annotations 让类型注解延后求值
from __future__ import annotations

# os：读 env vars（KE_DEFAULT_MODEL）；threading：缓存的线程安全
# typing：仅类型注解用
import logging
import os
import threading
from typing import Any

# Provider 类：复用 DashScope（qwen-plus）+ MiniMax（M2）现成实现
from src.service.qa_engine.llm_dashscope import DashScopeProvider
from src.service.qa_engine.llm_minimax import MiniMaxProvider


_log = logging.getLogger(__name__)


# ─── 支持的模型 catalog ─────────────────────────────────────────────────────
# 顺序即 UI 下拉默认排序；SUPPORTED_MODELS[0] 是 fallback 默认值
# 每项格式：(label, vendor) — label 是用户面 ID 也是 API 传输用的 key
SUPPORTED_MODELS: list[dict[str, str]] = [
    # qwen-plus：DashScope 主力，性价比好（决策点：默认）
    {"id": "qwen-plus", "label": "Qwen-Plus", "vendor": "DashScope"},
    # MiniMax-M2：MiniMax 新一代推理模型
    {"id": "MiniMax-M2", "label": "MiniMax-M2", "vendor": "MiniMax"},
]

# 默认 model id（用户首次登录、preferred_model 为空时用此值）
# env KE_DEFAULT_MODEL 可覆盖（运维切换默认值无需改代码）
DEFAULT_MODEL_ID = os.getenv("KE_DEFAULT_MODEL", SUPPORTED_MODELS[0]["id"])

# 模型 id → provider 实例的进程内缓存
# 同一进程 lifetime 内反复请求同一 model 复用 httpx client / 连接池
_provider_cache: dict[str, Any] = {}
# threading.Lock 保护缓存读写（虽然 asyncio 单线程，但 startup / shutdown
# 可能跨线程调；用 Lock 防御性兜底，开销忽略不计）
_cache_lock = threading.Lock()


def is_supported_model(model_id: str | None) -> bool:
    """判断 model_id 是否在 SUPPORTED_MODELS 白名单内。

    用于前后端校验：前端 dropdown 选项 / 后端写库前的 sanity check。
    None / 空字符串 / 未知 id → False。
    """
    if not model_id:
        return False
    # any + generator 表达式：早返，避免遍历所有项
    return any(m["id"] == model_id for m in SUPPORTED_MODELS)


def get_llm_provider(model_id: str | None = None) -> Any:
    """按 model_id 返回 provider 实例（带进程内缓存）。

    Args:
        model_id: 用户选的模型 id（来自 explain body / users.preferred_model）；
                  None / 空 / 未知 → 回退到 DEFAULT_MODEL_ID
    Returns:
        provider 实例（鸭子类型：保证有 complete/complete_with_tools/complete_stream/
        complete_stream_with_tools 与 .model 字段，与 DashScopeProvider 同形）
    Raises:
        RuntimeError: provider 构造失败且回退到默认也失败时（如 API key 缺失）

    设计：
      - 缓存：同一 model_id 整个进程 lifetime 复用同一 provider 实例
      - 兜底：未知 model_id 静默回退到默认（不抛错；UI 已在白名单内的话不会到这里）
      - 失败：默认 provider 也构造不出来才 raise（startup 期会触发）
    """
    # 1) 规范化 model_id：None / 空 / 未知 → 默认
    if not model_id or not is_supported_model(model_id):
        if model_id:
            _log.debug("get_llm_provider: unknown model_id=%r, fallback to default", model_id)
        model_id = DEFAULT_MODEL_ID

    # 2) 走缓存（线程安全，开销极小）
    with _cache_lock:
        cached = _provider_cache.get(model_id)
        if cached is not None:
            return cached

    # 3) 构造（不在锁内构造 —— 避免 httpx 初始化期阻塞其他 thread）
    provider = _build_provider(model_id)

    # 4) 写回缓存（再次取锁；若并发构造同 model，let "first write wins"）
    with _cache_lock:
        # 双检：可能在我们构造期另一线程已写入；用既有的避免重复 client
        existing = _provider_cache.get(model_id)
        if existing is not None:
            return existing
        _provider_cache[model_id] = provider
    return provider


def _build_provider(model_id: str) -> Any:
    """按 model_id 实例化对应 vendor 的 provider。

    分发逻辑：
      - qwen-* / 包含 qwen 的 model id → DashScopeProvider(model=model_id)
      - MiniMax-* / minimax-* → MiniMaxProvider(model=model_id)
      - 其他：回退到 DashScope（与默认一致）

    为什么用前缀匹配：未来 DashScope / MiniMax 加新型号（qwen-max / MiniMax-M3）
    时本工厂不用改代码 — 只要前端 SUPPORTED_MODELS 加新条目即可。
    """
    # 大小写不敏感比较（防用户配 KE_DEFAULT_MODEL=Qwen-Plus 之类）
    lower = model_id.lower()
    if lower.startswith("qwen"):
        # DashScopeProvider 的 model 参数即 DashScope API 的 model 字段
        return DashScopeProvider(model=model_id)
    if lower.startswith("minimax") or lower.startswith("abab"):
        return MiniMaxProvider(model=model_id)
    # fallback：未知前缀也走 DashScope（与 DEFAULT_MODEL_ID 同 vendor）
    _log.debug("_build_provider: unknown prefix for %r, falling back to DashScope", model_id)
    return DashScopeProvider(model=model_id)


def reset_cache() -> None:
    """清空 provider 缓存（测试 / 配置热更新用；生产不调）。"""
    with _cache_lock:
        _provider_cache.clear()
