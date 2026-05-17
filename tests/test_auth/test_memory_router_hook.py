"""_make_memory_writer 工厂测试（Fake DB/LLM，镜像 test_qa_session_title.py）。
设计：[[记忆系统-设计]] §6。
"""
import pytest

from src.service.qa_router import _make_memory_writer
from src.service.db_models_homepage import QAUserMemory, QASessionMemory, QAMessage, QAProjectMemory


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


@pytest.mark.asyncio
async def test_writer_compact_llm_failure_is_silent_and_noop():
    # 无触发词（不写用户记忆）+ 达阈值但 LLM 压缩抛错 → 静默吞掉，无任何写入、不抛
    class _BoomLLM:
        async def complete(self, *, system, user, **kw):
            raise RuntimeError("LLM down")

    db = _FakeDB(msg_rows=[_FakeMsg() for _ in range(6)])
    writer = _make_memory_writer(
        db=db, llm=_BoomLLM(), user_id=9, session_id="s1",
        question="下单流程怎么走",   # 无触发词
    )
    await writer()  # 必须不抛
    assert db.added == []
    assert db.committed is False


@pytest.mark.asyncio
async def test_writer_force_compact_threads_to_maybe_compact():
    db = _FakeDB(msg_rows=[_FakeMsg(), _FakeMsg(role="assistant")])
    writer = _make_memory_writer(
        db=db, llm=_FakeLLM(), user_id=3, session_id="s1",
        question="无触发词的普通问题", force_compact=True,
    )
    await writer()
    assert any(isinstance(o, QASessionMemory) for o in db.added)


@pytest.mark.asyncio
async def test_writer_project_trigger_writes_project_not_user():
    db = _FakeDB()
    writer = _make_memory_writer(
        db=db, llm=_FakeLLM(), user_id=3, session_id="s1",
        question="记住这个工程：orders_v2 是现行表", project_id="deposit",
    )
    await writer()
    proj = [o for o in db.added if isinstance(o, QAProjectMemory)]
    usr = [o for o in db.added if isinstance(o, QAUserMemory)]
    assert len(proj) == 1 and proj[0].content == "orders_v2 是现行表"
    assert proj[0].project_id == "deposit"
    assert usr == []


@pytest.mark.asyncio
async def test_writer_generic_trigger_still_user_level():
    db = _FakeDB()
    writer = _make_memory_writer(
        db=db, llm=_FakeLLM(), user_id=3, session_id="s1",
        question="记住我喜欢简短回答", project_id="deposit",
    )
    await writer()
    assert any(isinstance(o, QAUserMemory) for o in db.added)
    assert not any(isinstance(o, QAProjectMemory) for o in db.added)


@pytest.mark.asyncio
async def test_writer_project_id_none_no_crash():
    db = _FakeDB()
    writer = _make_memory_writer(
        db=db, llm=_FakeLLM(), user_id=3, session_id="s1",
        question="记住这个工程：X",
    )
    await writer()
    assert not any(isinstance(o, QAProjectMemory) for o in db.added)


@pytest.mark.asyncio
async def test_writer_project_write_failure_is_silent():
    # 工程写抛错 → 静默吞（_log.debug），_writer 不抛、绝不断答
    class _BoomDB:
        def __init__(self):
            self.added = []
        def add(self, obj):
            self.added.append(obj)
        async def commit(self):
            raise RuntimeError("db down")
        async def execute(self, stmt):
            class _R:
                def scalars(self_):
                    return self_
                def all(self_):
                    return []
                def one_or_none(self_):
                    return None
            return _R()
        async def get(self, *a, **k):
            return None

    db = _BoomDB()
    writer = _make_memory_writer(
        db=db, llm=_FakeLLM(), user_id=3, session_id="s1",
        question="记住这个工程：会崩的内容", project_id="deposit",
    )
    await writer()  # 必须不抛
