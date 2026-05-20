"""文件式记忆 S5：SessionCompactor + read_session_summary 单测。
设计：[[文件式记忆重构-设计]] §6。
沿用 tests/test_auth 既有 fake + tmp_path + @pytest.mark.asyncio 风格；
跨 S2/S4 fake stack 复用（_split_frontmatter/_render_frontmatter from memgen，
fake LLM 模式同 test_memory_extract.py）。
"""
from __future__ import annotations

import pytest

from src.service.memory.session import _summary_uri


def test_summary_uri_basic():
    """_summary_uri 拼正确的 ke:// URI（§6.2）。"""
    assert _summary_uri(7, "sess_abc") == "ke://u/7/session/sess_abc/summary.md"


def test_summary_uri_different_users_isolated():
    """不同 user_id 派生不同路径（S1 路径前缀隔离的基础）。"""
    u1 = _summary_uri(1, "s")
    u2 = _summary_uri(2, "s")
    assert u1 != u2
    assert "/u/1/" in u1 and "/u/2/" in u2


def test_summary_uri_different_sessions_isolated():
    """同 user 不同 session 派生不同路径。"""
    s1 = _summary_uri(7, "sess_a")
    s2 = _summary_uri(7, "sess_b")
    assert s1 != s2


from src.service.memory.session import read_session_summary
from src.service.memory.vfs import MemoryFS


@pytest.mark.asyncio
async def test_read_session_summary_not_exists_returns_empty(tmp_path):
    """summary.md 不存在 → 返 ""（与 recall_memory_block 同自包失败语义，§6.5）。"""
    # tmp_path 是 pytest 内置的临时目录 fixture；MemoryFS(root=...) 接收物理根
    fs = MemoryFS(root=str(tmp_path))
    # user 7 + sess_x 路径下没写过任何文件
    result = await read_session_summary(fs, user_id=7, session_id="sess_x")
    # 不存在 → 返 "" 让 composer 走零开销不注入路径
    assert result == ""
