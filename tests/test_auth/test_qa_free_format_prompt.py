"""自由格式 prompt（设计 §7）：AGENT_SYSTEM_PROMPT 不强制 6 段 JSON，但保留反幻觉 +
引用标记；build_user_prompt(free_format=True) 尾部任务块不再要求 JSON。"""
from src.service.qa_engine.prompts import (
    AGENT_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_user_prompt,
)


def test_agent_system_prompt_is_free_format():
    """AGENT_SYSTEM_PROMPT 去掉 6 段 JSON 强制，保留反幻觉 + 引用标记 + markdown 指引。"""
    assert "6 段式 JSON" not in AGENT_SYSTEM_PROMPT
    assert "必须合法 JSON" not in AGENT_SYSTEM_PROMPT
    assert "markdown" in AGENT_SYSTEM_PROMPT.lower()
    assert "不允许编造" in AGENT_SYSTEM_PROMPT or "不能编造" in AGENT_SYSTEM_PROMPT
    assert "[entity_id|显示文本]" in AGENT_SYSTEM_PROMPT
    assert AGENT_SYSTEM_PROMPT != SYSTEM_PROMPT


def test_build_user_prompt_free_format_drops_json_instruction():
    """free_format=True 时尾部任务块不再要求 6 段 / JSON。"""
    ctx = {"entry_candidates": [{"entity_id": "method//A", "summary_text": "x", "level": "api"}]}
    free = build_user_prompt("q", ctx, free_format=True)
    structured = build_user_prompt("q", ctx)

    assert "6 段式" not in free
    assert "严格按 JSON 输出" not in free
    assert "markdown" in free.lower()
    assert "6 段式" in structured
    assert "严格按 JSON 输出" in structured
    assert "method//A" in free and "method//A" in structured


def test_build_user_prompt_default_is_structured():
    """不传 free_format 默认 False（向后兼容，QASynthesizer 仍走 6 段）。"""
    ctx = {"entry_candidates": []}
    assert "6 段式" in build_user_prompt("q", ctx)
