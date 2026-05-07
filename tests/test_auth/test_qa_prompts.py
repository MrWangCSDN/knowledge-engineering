"""验证 prompts.py 的关键约束。

prompt 是字符串，没法用单测保证 LLM 输出质量；
但能保证 prompt **结构稳定** + **关键指令存在**。
"""
from src.service.qa_engine.prompts import (
    SYSTEM_PROMPT,
    build_user_prompt,
    build_user_prompt_with_history,
)


# ───────── SYSTEM_PROMPT ─────────

def test_system_prompt_contains_key_constraints():
    """LLM 必须看到的硬约束。"""
    assert "不允许编造" in SYSTEM_PROMPT
    assert "结构化输出" in SYSTEM_PROMPT
    assert "中文输出" in SYSTEM_PROMPT


def test_system_prompt_lists_6_section_types():
    for section_type in ["overview", "entry_point", "call_chain", "db_ops", "rules", "sources"]:
        assert section_type in SYSTEM_PROMPT, f"prompt 必须列出 {section_type}"


def test_system_prompt_specifies_reference_format():
    """LLM 知道用 [entity_id|text] 标记实体。"""
    assert "[entity_id|" in SYSTEM_PROMPT
    assert "kind" in SYSTEM_PROMPT


# ───────── build_user_prompt ─────────

def test_user_prompt_includes_question():
    ctx = {"entry_candidates": []}
    p = build_user_prompt("如何开户？", ctx)
    assert "如何开户？" in p
    assert "用户问题" in p


def test_user_prompt_lists_entry_candidates():
    ctx = {
        "entry_candidates": [
            {"entity_id": "method://m1", "summary_text": "开户主入口", "level": "api"},
            {"entity_id": "method://m2", "summary_text": "KYC 校验",   "level": "method"},
        ]
    }
    p = build_user_prompt("x", ctx)
    assert "method://m1" in p
    assert "开户主入口" in p
    assert "method://m2" in p


def test_user_prompt_truncates_long_summary():
    """summary 超 300 字符要截断（控制 token 数）。"""
    long_text = "x" * 500
    ctx = {"entry_candidates": [
        {"entity_id": "method://m", "summary_text": long_text, "level": "method"}
    ]}
    p = build_user_prompt("x", ctx)
    # 截断标记 …
    assert "…" in p


def test_user_prompt_lists_callees():
    ctx = {
        "entry_candidates": [{"entity_id": "method://entry", "summary_text": "x", "level": "api"}],
        "callees_by_entry": {"method://entry": ["method://child1", "method://child2"]},
    }
    p = build_user_prompt("x", ctx)
    assert "调用关系" in p
    assert "method://child1" in p
    assert "method://child2" in p


def test_user_prompt_lists_table_access():
    ctx = {
        "entry_candidates": [{"entity_id": "method://m", "summary_text": "x", "level": "method"}],
        "table_access_by_entry": {
            "method://m": [{"table_id": "user_account", "operation": "INSERT"}]
        },
    }
    p = build_user_prompt("x", ctx)
    assert "数据库访问" in p
    assert "user_account" in p
    assert "INSERT" in p


def test_user_prompt_no_candidates_message():
    """空 candidates 时给 LLM 明确信号，不要让它瞎编。"""
    p = build_user_prompt("x", {"entry_candidates": []})
    assert "未命中" in p or "未找到" in p


def test_user_prompt_omits_empty_sections():
    """空的 callees / callers / table_access 不应该污染 prompt。"""
    ctx = {
        "entry_candidates": [{"entity_id": "method://m", "summary_text": "x", "level": "method"}],
        "callees_by_entry": {"method://m": []},
        "callers_by_entry": {"method://m": []},
        "table_access_by_entry": {"method://m": []},
    }
    p = build_user_prompt("x", ctx)
    # 关键词不应该出现（因为对应数据全空）
    assert "调用关系" not in p
    assert "数据库访问" not in p


# ───────── 多轮对话 ─────────

def test_history_prompt_includes_recent_turns():
    ctx = {"entry_candidates": []}
    history = [
        {"role": "user", "content": "上一轮问的什么"},
        {"role": "assistant", "content": "上一轮的回答"},
    ]
    p = build_user_prompt_with_history("新问题", ctx, history=history)
    assert "对话历史" in p
    assert "上一轮的回答" in p
    assert "新问题" in p


def test_history_prompt_no_history_falls_back():
    ctx = {"entry_candidates": []}
    p_no = build_user_prompt_with_history("x", ctx, history=None)
    p_normal = build_user_prompt("x", ctx)
    assert p_no == p_normal


def test_history_prompt_truncates_to_last_10_messages():
    ctx = {"entry_candidates": []}
    # 15 条消息（应该只保留最近 10 条）
    history = [{"role": "user", "content": f"msg-{i}"} for i in range(15)]
    p = build_user_prompt_with_history("x", ctx, history=history)
    assert "msg-14" in p   # 最新的在
    assert "msg-0" not in p  # 最早的被截断
