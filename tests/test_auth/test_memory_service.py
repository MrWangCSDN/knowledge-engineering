"""记忆注入 + 服务逻辑测试（Fake DB/LLM，不起真 engine）。
设计：[[记忆系统-设计]] §6 §7
"""
import pytest

from src.service.qa_engine.synthesizer import QASynthesizer
from src.service.qa_engine.retriever import RetrievedContext


class _CapturingLLM:
    """记录最后一次 complete 的 system 入参。"""
    def __init__(self):
        self.last_system = None

    async def complete(self, *, system: str, user: str, **kw) -> str:
        self.last_system = system
        # 返回最简合法 6 段式，避免解析降级影响断言
        return '```json\n{"sections":[{"type":"overview",' \
               '"title":"t","content":"c","references":[]}]}\n```'


def _ctx(skill_id="architecture"):
    return RetrievedContext(
        question="下单流程怎么走",
        project_id="test-project",
        entry_candidates=[],
        callees_by_entry={},
        callers_by_entry={},
        table_access_by_entry={},
        skill_id=skill_id,
    )


@pytest.mark.asyncio
async def test_memory_block_injected_into_system():
    llm = _CapturingLLM()
    syn = QASynthesizer(llm)
    await syn.synthesize(_ctx(), memory_block="用户偏好：只看支付域")
    assert "用户偏好：只看支付域" in llm.last_system
    assert "企业代码知识分析师" in llm.last_system


@pytest.mark.asyncio
async def test_no_memory_block_keeps_system_unchanged():
    llm = _CapturingLLM()
    syn = QASynthesizer(llm)
    await syn.synthesize(_ctx())
    assert "记忆（关于本用户" not in llm.last_system


@pytest.mark.asyncio
async def test_memory_block_injected_in_chit_chat_path():
    llm = _CapturingLLM()
    syn = QASynthesizer(llm)
    await syn.synthesize(_ctx(skill_id="chit-chat"), memory_block="用户偏好：用 Java")
    assert "用户偏好：用 Java" in llm.last_system


class _CapturingStreamLLM:
    """记录最后一次调用的 system；同时支持 complete 与 complete_stream（async generator）。"""
    def __init__(self):
        self.last_system = None

    async def complete(self, *, system: str, user: str, **kw) -> str:
        self.last_system = system
        return '```json\n{"sections":[{"type":"overview",' \
               '"title":"t","content":"c","references":[]}]}\n```'

    async def complete_stream(self, *, system: str, user: str, **kw):
        self.last_system = system
        yield '```json\n{"sections":[{"type":"overview",' \
              '"title":"t","content":"c","references":[]}]}\n```'


@pytest.mark.asyncio
async def test_memory_block_injected_in_stream_path():
    llm = _CapturingStreamLLM()
    syn = QASynthesizer(llm)
    await syn.synthesize_stream(_ctx(), memory_block="用户偏好：流式注入")
    assert "用户偏好：流式注入" in llm.last_system
    assert "企业代码知识分析师" in llm.last_system


@pytest.mark.asyncio
async def test_memory_block_injected_in_chit_chat_stream_path():
    llm = _CapturingStreamLLM()
    syn = QASynthesizer(llm)
    await syn.synthesize_stream(
        _ctx(skill_id="chit-chat"), memory_block="用户偏好：流式 chit"
    )
    assert "用户偏好：流式 chit" in llm.last_system
