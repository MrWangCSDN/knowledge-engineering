"""方法/业务解读共用的单条处理：LLM → 向量 → 持久化，统一可恢复异常与 UI 回调。"""
from __future__ import annotations

import json
import logging
import urllib.error
from typing import Any, Callable

from src.core.domain_enums import InterpretPhase
from src.knowledge.base_interpretation_runner import BaseInterpretationRunner
from src.semantic.embedding import get_embedding

_LOG = logging.getLogger(__name__)

# 网络/解析等可预期失败：单条记失败并继续，不打 error 堆栈
INTERP_ITEM_RECOVERABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    urllib.error.URLError,
    TimeoutError,
    OSError,
    json.JSONDecodeError,
    KeyError,
)


def interpret_one_llm_embed_store(
    runner: BaseInterpretationRunner,
    label: str,
    phase: InterpretPhase,
    *,
    llm: Any,
    prompt: str,
    timeout: int,
    min_text_len: int,
    embedding_dim: int,
    persist: Callable[[str, list[float]], tuple[bool, bool]],
) -> tuple[int, int]:
    """
    单条：start_item → LLM → 最短文本检查 → embedding → persist(text, vec)。

    ``persist`` 返回 ``(success, created)``，与 ``Weaviate*Store.add_with_created`` 一致。

    Returns:
        ``(ok_delta, fail_delta)``，各为 0 或 1。
    """
    runner.start_item(label, phase)
    try:
        text = llm.generate(prompt, timeout=timeout)
        if not text or len(text) < min_text_len:
            runner.complete_item(label, False)
            return (0, 1)
        vec = get_embedding(text[:8000], embedding_dim)
        success, created = persist(text, vec)
        if success:
            runner.complete_item(label, created)
            return (1, 0)
        runner.complete_item(label, False)
        return (0, 1)
    except INTERP_ITEM_RECOVERABLE_EXCEPTIONS:
        runner.complete_item(label, False)
        return (0, 1)
    except Exception:
        _LOG.exception("解读单条失败（已计入失败） label=%r phase=%s", label, phase)
        runner.complete_item(label, False)
        return (0, 1)
