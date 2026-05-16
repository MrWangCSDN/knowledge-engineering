"""_make_memory_writer 工厂测试（Fake DB/LLM，镜像 test_qa_session_title.py）。
设计：[[记忆系统-设计]] §6。
"""
import pytest

from src.service.qa_router import _make_memory_writer
from src.service.db_models_homepage import QAUserMemory, QASessionMemory, QAMessage


class _FakeResult:
    def __init__(self, rows): self._rows = rows
    def scalars(self): return self
    def all(self): return self._rows
    def one_or_none(self): return self._rows[0] if self._rows else None


class _FakeDB:
    def __init__(self, msg_rows=None):
        self._msg_rows = msg_rows or []
        self.added = []
        self.committed = False

    async def execute(self, stmt):
        ent = stmt.column_descriptions[0]["entity"]
        if ent is QAMessage:
            return _FakeResult(self._msg_rows)
        return _FakeResult([])  # 无既有会话记忆

    def add(self, obj): self.added.append(obj)
    async def commit(self): self.committed = True
    async def get(self, model, pk): return None


class _FakeMsg:
    def __init__(self, role="user", content="问题"):
        self.role = role
        self.content = content


class _FakeLLM:
    async def complete(self, *, system, user, **kw):
        return "本次目标：排查下单；已确认瓶颈在 PaymentGateway"


@pytest.mark.asyncio
async def test_writer_persists_explicit_user_memory():
    db = _FakeDB()
    writer = _make_memory_writer(
        db=db, llm=_FakeLLM(), user_id=3, session_id="s1",
        question="记住我喜欢简短回答",
    )
    await writer()
    user_mems = [o for o in db.added if isinstance(o, QAUserMemory)]
    assert len(user_mems) == 1
    assert user_mems[0].content == "我喜欢简短回答"
    assert user_mems[0].user_id == 3


@pytest.mark.asyncio
async def test_writer_noop_when_no_trigger_and_below_threshold():
    db = _FakeDB(msg_rows=[_FakeMsg(), _FakeMsg()])  # 2 < 6
    writer = _make_memory_writer(
        db=db, llm=_FakeLLM(), user_id=3, session_id="s1",
        question="下单流程怎么走",
    )
    await writer()
    assert db.added == []


@pytest.mark.asyncio
async def test_writer_compacts_session_when_threshold_reached():
    db = _FakeDB(msg_rows=[_FakeMsg() for _ in range(6)])  # 达阈值
    writer = _make_memory_writer(
        db=db, llm=_FakeLLM(), user_id=3, session_id="s1",
        question="继续追问下一个问题",
    )
    await writer()
    sess_mems = [o for o in db.added if isinstance(o, QASessionMemory)]
    assert len(sess_mems) == 1
    assert "PaymentGateway" in sess_mems[0].working_summary


@pytest.mark.asyncio
async def test_writer_never_raises_on_llm_failure():
    class _BoomLLM:
        async def complete(self, *, system, user, **kw):
            raise RuntimeError("LLM down")

    db = _FakeDB(msg_rows=[_FakeMsg() for _ in range(6)])
    writer = _make_memory_writer(
        db=db, llm=_BoomLLM(), user_id=3, session_id="s1",
        question="记住我用 Java",
    )
    await writer()
    assert any(isinstance(o, QAUserMemory) for o in db.added)
