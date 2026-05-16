"""记忆注入 + 服务逻辑测试（Fake DB/LLM，不起真 engine）。
设计：[[记忆系统-设计]] §6 §7
"""
import pytest

from src.service.qa_engine.synthesizer import QASynthesizer
from src.service.qa_engine.retriever import RetrievedContext
from src.service.qa_engine.sse_emitter import stream_qa_answer
from src.service.memory.service import (
    detect_explicit_memory,
    recall_memory_block,
    write_explicit_memory,
    maybe_compact_session,
)
from src.service.db_models_homepage import QAUserMemory, QASessionMemory, QAMessage


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


# ───────── stream_qa_answer 透传 memory_block + on_memory ─────────


class _StubAnswer:
    sections = [{"type": "overview", "title": "t", "content": "c", "references": []}]
    token_usage = 1
    cost_yuan = 0.0
    raw_output = "c"


class _SpySynth:
    """记录 synthesize 收到的 memory_block。无 synthesize_stream → 走非流式兜底。"""
    def __init__(self):
        self.seen_memory_block = "UNSET"

    async def synthesize(self, ctx, *, history=None, memory_block=None):
        self.seen_memory_block = memory_block
        return _StubAnswer()


class _StubRetriever:
    async def retrieve(self, **kw):
        return RetrievedContext(
            question="q", project_id="p1", entry_candidates=[], callees_by_entry={},
            callers_by_entry={}, table_access_by_entry={}, skill_id="architecture",
        )


@pytest.mark.asyncio
async def test_stream_passes_memory_block_and_calls_on_memory():
    synth = _SpySynth()
    called = {"on_memory": False}

    async def _on_memory():
        called["on_memory"] = True

    chunks = []
    async for ev in stream_qa_answer(
        question="q", project_id="p1", session_id="s1",
        retriever=_StubRetriever(), synthesizer=synth, router=None,
        memory_block="用户偏好：简短", on_memory=_on_memory,
    ):
        chunks.append(ev)

    assert synth.seen_memory_block == "用户偏好：简短"
    assert called["on_memory"] is True
    assert any("event: done" in c for c in chunks)


# ───────── memory.service 逻辑（Fake DB/LLM）─────────


# --- detect_explicit_memory：纯函数，关键词起步 ---

def test_detect_trigger_strips_prefix():
    assert detect_explicit_memory("记住我喜欢简短的回答") == "我喜欢简短的回答"
    assert detect_explicit_memory("请记住：用 Java 不要 Kotlin") == "用 Java 不要 Kotlin"
    assert detect_explicit_memory("记一下 我关注支付域") == "我关注支付域"


def test_detect_no_trigger_returns_none():
    assert detect_explicit_memory("下单流程怎么走") is None
    assert detect_explicit_memory("解释下快排") is None


def test_detect_trigger_but_empty_content_returns_none():
    assert detect_explicit_memory("记住") is None
    assert detect_explicit_memory("记住：   ") is None


# --- Fake DB / LLM ---

class _FakeResult:
    def __init__(self, rows): self._rows = rows
    def scalars(self): return self
    def all(self): return self._rows
    def one_or_none(self): return self._rows[0] if self._rows else None


class _FakeMsg:
    def __init__(self, role="user", content="问题"):
        self.role = role
        self.content = content


class _FakeMemDB:
    def __init__(self, user_rows=None, session_row=None, msg_rows=None):
        self._user_rows = user_rows or []
        self._session_row = session_row
        self._msg_rows = msg_rows or []
        self.added = []
        self.committed = False

    async def execute(self, stmt):
        ent = stmt.column_descriptions[0]["entity"]
        if ent is QAUserMemory:
            return _FakeResult(self._user_rows)
        if ent is QASessionMemory:
            return _FakeResult([self._session_row] if self._session_row else [])
        return _FakeResult(self._msg_rows)

    def add(self, obj): self.added.append(obj)
    async def commit(self): self.committed = True
    async def get(self, model, pk): return self._session_row


class _FakeMemLLM:
    def __init__(self, reply="本次目标：排查下单超时；已确认瓶颈在 PaymentGateway"):
        self._reply = reply
    async def complete(self, *, system, user, **kw): return self._reply


@pytest.mark.asyncio
async def test_recall_empty_when_nothing():
    db = _FakeMemDB()
    block = await recall_memory_block(db, user_id=1, session_id="s1")
    assert block == ""


@pytest.mark.asyncio
async def test_recall_combines_session_then_user():
    um = QAUserMemory(user_id=1, kind="preference", content="回答简短",
                       source="explicit", status="active")
    sm = QASessionMemory(session_id="s1", working_summary="已确认瓶颈在网关",
                         turn_count=6)
    db = _FakeMemDB(user_rows=[um], session_row=sm)
    block = await recall_memory_block(db, user_id=1, session_id="s1")
    assert "已确认瓶颈在网关" in block
    assert "回答简短" in block
    assert block.index("已确认瓶颈在网关") < block.index("回答简短")


@pytest.mark.asyncio
async def test_write_explicit_adds_user_memory_row():
    db = _FakeMemDB()
    await write_explicit_memory(db, user_id=7, session_id="s1", content="我用 Java")
    assert len(db.added) == 1
    row = db.added[0]
    assert isinstance(row, QAUserMemory)
    assert row.user_id == 7 and row.content == "我用 Java"
    assert row.kind == "preference" and row.source == "explicit"
    assert row.source_session_id == "s1"
    assert db.committed is True


@pytest.mark.asyncio
async def test_compact_skips_below_threshold():
    sm = QASessionMemory(session_id="s1", working_summary="old", turn_count=0)
    db = _FakeMemDB(session_row=sm, msg_rows=[_FakeMsg(), _FakeMsg()])
    await maybe_compact_session(db, _FakeMemLLM(), session_id="s1", every_n_messages=6)
    assert db.committed is False


@pytest.mark.asyncio
async def test_compact_creates_summary_when_threshold_reached():
    db = _FakeMemDB(session_row=None, msg_rows=[_FakeMsg() for _ in range(6)])
    await maybe_compact_session(db, _FakeMemLLM(), session_id="s1", every_n_messages=6)
    assert len(db.added) == 1
    row = db.added[0]
    assert isinstance(row, QASessionMemory)
    assert row.session_id == "s1"
    assert "PaymentGateway" in row.working_summary
    assert db.committed is True
