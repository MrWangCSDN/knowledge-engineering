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
