"""方法/业务解读共用的单条处理：LLM → 清洗 → 向量化(仅摘要) → 持久化。"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
from typing import Any, Callable

from src.core.domain_enums import InterpretPhase
from src.knowledge.base_interpretation_runner import BaseInterpretationRunner
from src.semantic.embedding import get_embedding

_LOG = logging.getLogger(__name__)

SUMMARY_PREFIX = "[摘要]"
DETAIL_PREFIX = "[详情]"

# 网络/解析等可预期失败：单条记失败并继续，不打 error 堆栈
INTERP_ITEM_RECOVERABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    urllib.error.URLError,
    TimeoutError,
    OSError,
    json.JSONDecodeError,
    KeyError,
)


def clean_think_tags(text: str) -> str:
    """去除 LLM 输出中的 <think>...</think> 思维链内容。"""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def extract_summary(text: str) -> str:
    """从 LLM 输出中提取 [摘要] 部分。找不到时取首行前50字。"""
    if SUMMARY_PREFIX in text:
        after = text.split(SUMMARY_PREFIX, 1)[1]
        # 取到 [详情] 之前，或第一个换行之前
        if DETAIL_PREFIX in after:
            summary = after.split(DETAIL_PREFIX, 1)[0]
        else:
            summary = after.split("\n", 1)[0]
        summary = summary.strip()
        if len(summary) > 50:
            summary = summary[:50]
        if summary:
            return summary
    # 回退：取第一个有内容的行
    for line in text.strip().split("\n"):
        line = line.strip().lstrip("#*- ")
        if len(line) >= 5:
            return line[:50]
    return text[:50]


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
    单条：start_item → LLM → 清洗(去think) → 提取摘要 → embedding(仅摘要) → persist。

    LLM 输出格式要求:
      [摘要] 关键词1 关键词2 ... (≤50字)
      [详情]
      完整技术解读...

    向量化只对 [摘要] 部分做 embedding，保证搜索精度。
    完整文本（含摘要+详情）存入 Weaviate，供后续链路分析使用。

    Returns:
        ``(ok_delta, fail_delta)``，各为 0 或 1。
    """
    runner.start_item(label, phase)
    try:
        raw_text = llm.generate(prompt, timeout=timeout)
        if not raw_text or len(raw_text) < min_text_len:
            runner.complete_item(label, False)
            return (0, 1)

        # 1. 去除 <think> 思维链
        text = clean_think_tags(raw_text)
        if len(text) < min_text_len:
            runner.complete_item(label, False)
            return (0, 1)

        # 2. 提取摘要关键词
        summary = extract_summary(text)

        # 3. 只对摘要做 embedding（短文本，高信息密度）
        vec = get_embedding(summary, embedding_dim)

        # 4. 持久化（完整文本 + 摘要向量）
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
