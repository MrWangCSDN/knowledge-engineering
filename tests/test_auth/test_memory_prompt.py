"""with_memory_block 纯函数契约 + 会话压缩 prompt 存在性。
设计：[[记忆系统-设计]] §7（记忆块注入 system 顶部）
"""
from src.service.qa_engine.prompts import (
    with_memory_block,
    _SESSION_COMPACT_SYSTEM,
)

BASE = "你是企业代码知识分析师。"


def test_none_block_returns_system_unchanged():
    assert with_memory_block(BASE, None) == BASE


def test_empty_block_returns_system_unchanged():
    assert with_memory_block(BASE, "   ") == BASE


def test_block_prepended_with_delimiter_and_system_kept():
    out = with_memory_block(BASE, "用户偏好：回答简短")
    assert "用户偏好：回答简短" in out
    assert BASE in out
    # 记忆块在 system 之前（注入顶部，优先级最高）
    assert out.index("用户偏好：回答简短") < out.index(BASE)
    # 有明确分隔标记，避免与正文混淆
    assert "记忆" in out


def test_compact_system_prompt_exists_and_nonempty():
    assert isinstance(_SESSION_COMPACT_SYSTEM, str)
    assert len(_SESSION_COMPACT_SYSTEM) > 20
