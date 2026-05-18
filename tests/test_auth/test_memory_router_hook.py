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


# ───────── §22：端到端 用户级 detect→parse→write ─────────
from src.service.db_models_homepage import QAUserMemory as _QUM


class _SeedDB(_FakeDB):
    """在 _FakeDB 基础上让 QAUserMemory 查询返回种子行（验证 identity 归档）。"""
    def __init__(self, user_rows=None, msg_rows=None):
        super().__init__(msg_rows=msg_rows)
        self._user_rows = user_rows or []

    async def execute(self, stmt):
        ent = stmt.column_descriptions[0]["entity"]
        if ent is _QUM:
            return _FakeResult(self._user_rows)
        return await super().execute(stmt)


class _IntentLLM:
    """complete：意图解析调用（system 含『意图解析器』）时返回预设；否则返回压缩占位。"""
    def __init__(self, intent_json): self._j = intent_json
    async def complete(self, *, system, user, **kw):
        if "意图解析器" in system:
            return self._j
        return "本次目标：x"


def _seed_identity(content="用户的名字是王山河"):
    return _QUM(user_id=3, kind="identity", content=content,
                source="explicit", source_session_id="s0", status="active")


@pytest.mark.asyncio
async def test_writer_suffix_identity_supersedes_end_to_end():
    old = _seed_identity()
    db = _SeedDB(user_rows=[old])
    llm = _IntentLLM('{"tier":"user","kind":"identity",'
                     '"content":"用户的名字是李龙飞","supersedes_kind":"identity"}')
    writer = _make_memory_writer(
        db=db, llm=llm, user_id=3, session_id="s1",
        question="我改名叫李龙飞 请记住",          # 句尾触发词
    )
    await writer()
    assert old.status == "archived"
    new = [o for o in db.added if isinstance(o, _QUM)]
    assert len(new) == 1 and new[0].kind == "identity"
    assert new[0].content == "用户的名字是李龙飞" and new[0].status == "active"


@pytest.mark.asyncio
async def test_writer_skip_writes_nothing():
    db = _SeedDB()
    llm = _IntentLLM('{"tier":"skip","kind":"preference","content":"x","supersedes_kind":null}')
    writer = _make_memory_writer(
        db=db, llm=llm, user_id=3, session_id="s1", question="记住 嗯嗯",
    )
    await writer()
    assert [o for o in db.added if isinstance(o, _QUM)] == []


@pytest.mark.asyncio
async def test_writer_parse_failure_falls_back_preference():
    db = _SeedDB()
    # 意图解析返回非 JSON → parse 兜底 preference 原样写
    writer = _make_memory_writer(
        db=db, llm=_IntentLLM("好的"), user_id=3, session_id="s1",
        question="记住我喜欢简短回答",
    )
    await writer()
    rows = [o for o in db.added if isinstance(o, _QUM)]
    assert len(rows) == 1
    assert rows[0].kind == "preference" and rows[0].content == "我喜欢简短回答"


@pytest.mark.asyncio
async def test_writer_valid_json_preference_structured_path():
    # 结构化路径（非兜底）：parse 成功返回 kind=preference → 按解析结果写，
    # 区别于 test_writer_parse_failure_falls_back_preference（那条走兜底）。
    db = _SeedDB()
    llm = _IntentLLM('{"tier":"user","kind":"preference",'
                     '"content":"用户偏好简短回答","supersedes_kind":null}')
    writer = _make_memory_writer(
        db=db, llm=llm, user_id=3, session_id="s1",
        question="记住我喜欢啰嗦冗长的解释",   # 原话与解析后 content 不同 → 证明走的是解析结果而非兜底/原文
    )
    await writer()
    rows = [o for o in db.added if isinstance(o, _QUM)]
    assert len(rows) == 1
    assert rows[0].kind == "preference"
    assert rows[0].content == "用户偏好简短回答"   # = 解析结果，非原话/兜底
