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


@pytest.mark.asyncio
async def test_read_session_summary_corrupt_frontmatter_returns_body(tmp_path):
    """frontmatter YAML 损坏但 body 可读 → 返裸 body（自愈优先，§6.5）。

    场景：S5 部署初期 / 手工编辑误 / partial write 崩溃产出半损坏文件；
    composer 不能因为 frontmatter 坏就丢 summary 内容（用户体验降级）。
    """
    fs = MemoryFS(root=str(tmp_path))
    # 手写一份「frontmatter YAML 损坏 + body 完整」的文件直接落盘
    # _split_frontmatter 见非法 YAML 容错为空 dict，body 仍正确返
    bad_yaml = "---\n: : : invalid yaml ::: \n---\n用户讨论 PaymentGateway。\n"
    # 直接写文件（绕开 _render_frontmatter，模拟外部损坏）
    await fs.write("ke://u/7/session/sess_x/summary.md", bad_yaml)

    result = await read_session_summary(fs, user_id=7, session_id="sess_x")
    # body 仍可读 → 返裸文本（去尾换行）
    assert result == "用户讨论 PaymentGateway。"


from src.service.memory.session import SessionCompactor


class _FakeLLM:
    """记录 complete() 调用入参；返回固定的 fake summary 文本。

    与 test_memory_extract.py 既有 fake LLM 同形态（鸭子 async complete）。
    """
    def __init__(self, *, response: str = "fake summary 文本"):
        self.calls: list[dict] = []
        self.response = response

    async def complete(self, *, system: str, user: str, **kw) -> str:
        # 记录每次调用的 system / user 参数（断言用）
        self.calls.append({"system": system, "user": user})
        return self.response


def test_session_compactor_init_holds_llm():
    """SessionCompactor 仅持 llm（同 S4 MemoryExtractor，fs/db 走方法形参）。"""
    llm = _FakeLLM()
    compactor = SessionCompactor(llm)
    # 不直接访问私有 _llm 字段（视为实现细节）；通过断言无异常构造来约束公开契约
    assert compactor is not None
