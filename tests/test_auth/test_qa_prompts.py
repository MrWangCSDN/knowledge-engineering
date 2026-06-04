"""验证 prompts.py 的关键约束。

prompt 是字符串，没法用单测保证 LLM 输出质量；
但能保证 prompt **结构稳定** + **关键指令存在**。
"""
from src.service.qa_engine.prompts import (
    SYSTEM_PROMPT,
    _format_history,
    build_chitchat_user_prompt,
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


# ───────── 会话级多轮：_format_history / build_chitchat_user_prompt（spec §20）─────────

def test_format_history_basic_and_truncation():
    h = [{"role": "user", "content": "x" * 250}, {"role": "assistant", "content": "好的"}]
    out = _format_history(h)
    assert out == f"[user] {'x' * 200}\n[assistant] 好的"


def test_format_history_keeps_last_10():
    h = [{"role": "user", "content": f"m{i}"} for i in range(15)]
    out = _format_history(h)
    assert out.count("\n") == 9
    assert "[user] m5" in out and "[user] m14" in out
    assert "[user] m4" not in out


def test_format_history_defensive():
    assert _format_history(None) == ""
    assert _format_history([]) == ""
    assert _format_history("notlist") == ""
    assert _format_history([{"role": "user", "content": "ok"}, 123, None]) == "[user] ok"
    assert _format_history([{}]) == "[?] "


def test_build_chitchat_user_prompt_with_history():
    h = [{"role": "user", "content": "我喜欢吃西瓜"}, {"role": "assistant", "content": "西瓜解暑"}]
    out = build_chitchat_user_prompt("我喜欢什么水果", h)
    assert out == "【对话历史】\n[user] 我喜欢吃西瓜\n[assistant] 西瓜解暑\n\n我喜欢什么水果"


def test_build_chitchat_user_prompt_no_history_is_bare_question():
    assert build_chitchat_user_prompt("你好", None) == "你好"
    assert build_chitchat_user_prompt("你好", []) == "你好"


def test_format_history_none_content_or_role_no_crash():
    # 真缺陷回归（用户实测 "LLM 调用失败：'NoneType' object is not subscriptable"）：
    # history 项是 dict 但 content/role 值为 None（键存在、值为 None，非键缺失）——
    # dict.get(k, default) 仅在键缺失时用 default，值为 None 时返回 None，
    # 旧代码 None[:200] 抛 TypeError。content=null 的消息很常见（出错/结构化回答）。
    assert _format_history([{"role": "assistant", "content": None}]) == "[assistant] "
    assert _format_history([{"role": None, "content": None}]) == "[?] "
    assert _format_history(
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": None}]
    ) == "[user] hi\n[assistant] "


def test_build_chitchat_user_prompt_history_with_none_content_no_crash():
    h = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": None}]
    out = build_chitchat_user_prompt("你好", h)
    assert out == "【对话历史】\n[user] hi\n[assistant] \n\n你好"


def test_build_user_prompt_with_history_byte_identical_after_refactor():
    ctx = {"entry_candidates": [], "skill_id": "architecture"}
    h = [{"role": "user", "content": "Q1"}, {"role": "assistant", "content": "A1"}]
    base = build_user_prompt("问题", ctx)
    expected = f"【对话历史】\n[user] Q1\n[assistant] A1\n\n{base}"
    assert build_user_prompt_with_history("问题", ctx, history=h) == expected
    assert build_user_prompt_with_history("问题", ctx, history=None) == base


# ───────── §21：_SESSION_COMPACT_SYSTEM 忠实对话摘要 characterization ─────────
from src.service.qa_engine.prompts import _SESSION_COMPACT_SYSTEM


def test_session_compact_system_is_faithful_digest_not_taskonly():
    s = _SESSION_COMPACT_SYSTEM
    assert "忠实保留" in s
    assert "不得丢弃" in s and "【已有会话摘要】" in s
    assert "时间线" in s
    assert "300" in s
    assert "会话工作状态压缩器" not in s
    assert "本次会话目标" not in s
    assert "150" not in s


# ───────── 逻辑图中文化：调用链方法业务解读 + A1 指令 ─────────


def test_build_user_prompt_renders_callchain_summaries_and_a1():
    """callchain_node_summaries 非空 → 渲染「调用链方法业务解读」块 + A1 业务流指令。"""
    ctx = {
        "skill_id": "architecture",
        "entry_candidates": [{"entity_id": "C::register#(p)", "summary_text": "s", "level": "method"}],
        "call_edges_by_entry": {"C::register#(p)": [("C::register#(p)", "B::save#(m)")]},
        "callchain_node_summaries": {
            "C::register#(p)": "会员注册入口，校验后落库",
            "B::save#(m)": "写入会员表",
        },
    }
    out = build_user_prompt("注册流程", ctx)
    # 解读块标题 + 至少一条解读 + entityId（method:// scheme）
    assert "调用链方法业务解读" in out
    assert "会员注册入口，校验后落库" in out
    assert "method://C::register#(p)" in out
    # A1 指令关键词
    assert "锚定" in out and "严禁虚构" in out


def test_build_user_prompt_no_summary_block_when_empty():
    """callchain_node_summaries 为空 → 不渲染解读块（不空标题）。"""
    ctx = {
        "skill_id": "architecture",
        "entry_candidates": [{"entity_id": "C::register#(p)", "summary_text": "s", "level": "method"}],
        "call_edges_by_entry": {"C::register#(p)": [("C::register#(p)", "B::save#(m)")]},
        "callchain_node_summaries": {},
    }
    out = build_user_prompt("注册流程", ctx)
    assert "调用链方法业务解读" not in out

