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
    detect_explicit_project_memory,
    write_explicit_project_memory,
    parse_user_memory_intent,
)
from src.service.db_models_homepage import QAUserMemory, QASessionMemory, QAMessage, QAProjectMemory


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
    def __init__(self, role="user", content="问题", msg_metadata=None):
        self.role = role
        self.content = content
        self.msg_metadata = msg_metadata


class _FakeMemDB:
    def __init__(self, user_rows=None, session_row=None, msg_rows=None, project_rows=None):
        self._user_rows = user_rows or []
        self._session_row = session_row
        self._msg_rows = msg_rows or []
        self._project_rows = project_rows or []
        self.added = []
        self.committed = False

    async def execute(self, stmt):
        ent = stmt.column_descriptions[0]["entity"]
        if ent is QAUserMemory:
            return _FakeResult(self._user_rows)
        if ent is QASessionMemory:
            return _FakeResult([self._session_row] if self._session_row else [])
        if ent is QAProjectMemory:
            return _FakeResult(self._project_rows)
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


@pytest.mark.asyncio
async def test_compact_skips_when_no_new_messages_since_last():
    # turn_count == msg_count → 距上次压缩零新增，跳过（不调 LLM、不 commit）。
    # 同时是 Important-1（过阈后每轮都压）的回归守卫。
    sm = QASessionMemory(session_id="s1", working_summary="prev", turn_count=6)
    db = _FakeMemDB(session_row=sm, msg_rows=[_FakeMsg() for _ in range(6)])
    await maybe_compact_session(db, _FakeMemLLM(), session_id="s1", every_n_messages=6)
    assert db.committed is False
    assert db.added == []


# ───────── 会话级 focus_entity_ids 抽取（spec §17）─────────

from src.service.memory.service import _extract_focus_entity_ids


class _FakeMsgMeta:
    """带 msg_metadata 的 fake 消息（assistant 轮）。"""
    def __init__(self, role="assistant", content="答", msg_metadata=None):
        self.role = role
        self.content = content
        self.msg_metadata = msg_metadata


def test_extract_focus_dedup_and_order_and_cap():
    msgs = [
        _FakeMsgMeta(msg_metadata={"cited_entities": ["method://a", "class://b"]}),
        _FakeMsgMeta(msg_metadata={"cited_entities": ["method://a"],
                                   "entry_points": ["method://c"]}),
        _FakeMsgMeta(msg_metadata={"cited_entities": [f"method://x{i}" for i in range(20)]}),
    ]
    out = _extract_focus_entity_ids(msgs)
    assert out[:3] == ["method://a", "class://b", "method://c"]
    assert len(out) == 10


def test_extract_focus_defensive_on_missing_or_bad_metadata():
    class _Bare:
        role = "user"; content = "q"
    msgs = [
        _Bare(),
        _FakeMsgMeta(role="user", content="q", msg_metadata=None),
        _FakeMsgMeta(msg_metadata="not-a-dict"),
        _FakeMsgMeta(msg_metadata={"cited_entities": "not-a-list"}),
        _FakeMsgMeta(msg_metadata={"entry_points": None}),
    ]
    assert _extract_focus_entity_ids(msgs) == []


def test_extract_focus_filters_empty_and_nonstr():
    msgs = [_FakeMsgMeta(msg_metadata={"cited_entities": ["method://ok", "", None, 123]})]
    assert _extract_focus_entity_ids(msgs) == ["method://ok"]


@pytest.mark.asyncio
async def test_compact_persists_focus_entity_ids_new_row():
    msgs = [_FakeMsg() for _ in range(5)] + [
        _FakeMsg(role="assistant", content="答",
                 msg_metadata={"cited_entities": ["method://pay", "table://orders"]}),
    ]
    db = _FakeMemDB(session_row=None, msg_rows=msgs)
    await maybe_compact_session(db, _FakeMemLLM(), session_id="s1", every_n_messages=6)
    row = [o for o in db.added if isinstance(o, QASessionMemory)][0]
    assert row.focus_entity_ids == ["method://pay", "table://orders"]
    assert row.working_summary


@pytest.mark.asyncio
async def test_compact_updates_focus_entity_ids_existing_row():
    sm = QASessionMemory(session_id="s1", working_summary="old",
                         turn_count=0, focus_entity_ids=["method://old"])
    msgs = [_FakeMsg() for _ in range(5)] + [
        _FakeMsg(role="assistant",
                 msg_metadata={"cited_entities": ["method://new"]}),
    ]
    db = _FakeMemDB(session_row=sm, msg_rows=msgs)
    await maybe_compact_session(db, _FakeMemLLM(), session_id="s1", every_n_messages=6)
    assert sm.focus_entity_ids == ["method://new"]
    assert db.committed is True


@pytest.mark.asyncio
async def test_recall_includes_focus_entities_in_session_block():
    sm = QASessionMemory(session_id="s1", working_summary="已确认瓶颈在网关",
                         turn_count=6, focus_entity_ids=["method://pay", "table://orders"])
    db = _FakeMemDB(user_rows=[], session_row=sm)
    block = await recall_memory_block(db, user_id=1, session_id="s1")
    assert "已确认瓶颈在网关" in block
    assert "【本次聚焦实体】" in block
    assert "method://pay" in block and "table://orders" in block
    assert block.index("已确认瓶颈在网关") < block.index("【本次聚焦实体】")


@pytest.mark.asyncio
async def test_recall_no_focus_line_when_empty_or_none():
    sm1 = QASessionMemory(session_id="s1", working_summary="x", turn_count=6,
                          focus_entity_ids=[])
    sm2 = QASessionMemory(session_id="s2", working_summary="y", turn_count=6,
                          focus_entity_ids=None)
    for sid, sm in (("s1", sm1), ("s2", sm2)):
        db = _FakeMemDB(user_rows=[], session_row=sm)
        block = await recall_memory_block(db, user_id=1, session_id=sid)
        assert "【本次聚焦实体】" not in block


@pytest.mark.asyncio
async def test_compact_force_bypasses_n_floor():
    db = _FakeMemDB(session_row=None, msg_rows=[_FakeMsg(), _FakeMsg(role="assistant")])
    await maybe_compact_session(db, _FakeMemLLM(), session_id="s1",
                                every_n_messages=6, force=True)
    assert any(isinstance(o, QASessionMemory) for o in db.added)
    assert db.committed is True


@pytest.mark.asyncio
async def test_compact_force_still_skips_when_nothing_new():
    sm = QASessionMemory(session_id="s1", working_summary="prev", turn_count=2)
    db = _FakeMemDB(session_row=sm, msg_rows=[_FakeMsg(), _FakeMsg(role="assistant")])
    await maybe_compact_session(db, _FakeMemLLM(), session_id="s1",
                                every_n_messages=6, force=True)
    assert db.committed is False
    assert db.added == []


@pytest.mark.asyncio
async def test_compact_non_force_unchanged_below_floor():
    db = _FakeMemDB(session_row=None, msg_rows=[_FakeMsg(), _FakeMsg()])
    await maybe_compact_session(db, _FakeMemLLM(), session_id="s1", every_n_messages=6)
    assert db.added == []


@pytest.mark.asyncio
async def test_stream_meta_carries_context_usage():
    synth = _SpySynth()
    cu = {"used_tokens": 1234, "window_tokens": 1000000, "pct": 1.0,
          "history_trimmed": True}
    chunks = []
    async for ev in stream_qa_answer(
        question="q", project_id="p1", session_id="s1",
        retriever=_StubRetriever(), synthesizer=synth, router=None,
        context_usage=cu,
    ):
        chunks.append(ev)
    meta = [c for c in chunks if c.startswith("event: meta")][0]
    assert '"context_usage"' in meta and '"history_trimmed":true' in meta
    assert '"window_tokens":1000000' in meta


@pytest.mark.asyncio
async def test_stream_meta_no_context_usage_when_none():
    synth = _SpySynth()
    chunks = []
    async for ev in stream_qa_answer(
        question="q", project_id="p1", session_id="s1",
        retriever=_StubRetriever(), synthesizer=synth, router=None,
    ):
        chunks.append(ev)
    meta = [c for c in chunks if c.startswith("event: meta")][0]
    assert "context_usage" not in meta


# ───────── 工程级 S1（spec §19）─────────


def test_detect_project_trigger_strips_prefix():
    assert detect_explicit_project_memory("记住这个工程：orders_v2 是现行表") == "orders_v2 是现行表"
    assert detect_explicit_project_memory("记住本工程 用 Java 21") == "用 Java 21"
    assert detect_explicit_project_memory("工程记住：回调有重试") == "回调有重试"


def test_detect_project_no_trigger_or_empty():
    assert detect_explicit_project_memory("记住我喜欢简短") is None
    assert detect_explicit_project_memory("下单流程怎么走") is None
    assert detect_explicit_project_memory("记住这个工程：   ") is None


@pytest.mark.asyncio
async def test_write_explicit_project_adds_row():
    db = _FakeMemDB()
    await write_explicit_project_memory(
        db, project_id="deposit", user_id=7, session_id="s1", content="orders_v2 现行表"
    )
    rows = [o for o in db.added if isinstance(o, QAProjectMemory)]
    assert len(rows) == 1
    r = rows[0]
    assert r.project_id == "deposit" and r.user_id == 7
    assert r.content == "orders_v2 现行表"
    assert r.scope == "private" and r.source == "explicit" and r.status == "active"
    assert r.source_session_id == "s1"
    assert db.committed is True


@pytest.mark.asyncio
async def test_recall_includes_project_block_after_user_block():
    pm = QAProjectMemory(project_id="deposit", user_id=1, scope="private",
                          content="orders_v2 是现行表", source="explicit", status="active")
    um = QAUserMemory(user_id=1, kind="preference", content="回答简短",
                       source="explicit", status="active")
    db = _FakeMemDB(user_rows=[um], session_row=None, project_rows=[pm])
    block = await recall_memory_block(db, user_id=1, session_id="s1", project_id="deposit")
    assert "回答简短" in block and "orders_v2 是现行表" in block
    assert "【工程记忆】" in block
    assert block.index("回答简短") < block.index("orders_v2 是现行表")


@pytest.mark.asyncio
async def test_recall_no_project_block_when_project_id_none():
    pm = QAProjectMemory(project_id="deposit", user_id=1, scope="private",
                          content="X", source="explicit", status="active")
    db = _FakeMemDB(user_rows=[], session_row=None, project_rows=[pm])
    block = await recall_memory_block(db, user_id=1, session_id="s1")
    assert "【工程记忆】" not in block


@pytest.mark.asyncio
async def test_recall_project_tenant_filter_real_sqlite():
    """真 SQLite 验证多租户隔离 SQL（_FakeMemDB 绕过 WHERE，安全属性须真查）。
    4 条隔离轴：跨工程 / 他人 private 不可见 / team 可见 / archived 排除。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from src.service.db import Base

    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SM = async_sessionmaker(eng, expire_on_commit=False)
    async with SM() as s:
        s.add_all([
            QAProjectMemory(id=1, project_id="A", user_id=1, scope="private",
                            content="A-own-private", source="explicit", status="active"),
            QAProjectMemory(id=2, project_id="A", user_id=2, scope="private",
                            content="A-other-private", source="explicit", status="active"),
            QAProjectMemory(id=3, project_id="A", user_id=2, scope="team",
                            content="A-team", source="explicit", status="active"),
            QAProjectMemory(id=4, project_id="B", user_id=1, scope="private",
                            content="B-own-private", source="explicit", status="active"),
            QAProjectMemory(id=5, project_id="A", user_id=1, scope="private",
                            content="A-own-archived", source="explicit", status="archived"),
        ])
        await s.commit()
    async with SM() as s:
        block = await recall_memory_block(s, user_id=1, session_id="x", project_id="A")
    # 可见：自己在 A 的 active private + A 的 team
    assert "A-own-private" in block
    assert "A-team" in block
    # 不可见：他人 A private / 跨工程 B / 自己 archived
    assert "A-other-private" not in block
    assert "B-own-private" not in block
    assert "A-own-archived" not in block
    await eng.dispose()


@pytest.mark.asyncio
async def test_recall_project_block_caps_at_limit_real_sqlite():
    """真 SQLite：>20 条 active 工程记忆，recall 只取 _PROJECT_MEMORY_LIMIT(20) 条
    （回归守护 .limit()，防被误删导致 prompt 膨胀/成本失控）。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from src.service.db import Base

    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SM = async_sessionmaker(eng, expire_on_commit=False)
    async with SM() as s:
        s.add_all([
            QAProjectMemory(
                id=i, project_id="A", user_id=1, scope="private",
                content=f"note-{i}", source="explicit", status="active",
            )
            for i in range(1, 26)  # 25 条
        ])
        await s.commit()
    async with SM() as s:
        block = await recall_memory_block(s, user_id=1, session_id="x", project_id="A")
    # 工程块每条是 "- note-i" 一行；统计 "- note-" 出现次数
    assert block.count("- note-") == 20
    await eng.dispose()


# ───────── chit-chat 会话级多轮：history 接入（spec §20）─────────

class _CapUserLLM:
    """记录最后一次 complete/complete_stream 的 user 入参。"""
    def __init__(self):
        self.last_user = None

    async def complete(self, *, system, user, **kw):
        self.last_user = user
        return "ok"

    async def complete_stream(self, *, system, user, **kw):
        self.last_user = user
        yield "ok"


_HIST = [
    {"role": "user", "content": "我喜欢吃西瓜"},
    {"role": "assistant", "content": "西瓜解暑"},
]


@pytest.mark.asyncio
async def test_chitchat_sync_includes_history():
    llm = _CapUserLLM()
    syn = QASynthesizer(llm)
    await syn.synthesize(_ctx(skill_id="chit-chat"), history=_HIST)
    assert "【对话历史】" in llm.last_user
    assert "我喜欢吃西瓜" in llm.last_user
    assert llm.last_user.endswith("下单流程怎么走")


@pytest.mark.asyncio
async def test_chitchat_stream_includes_history():
    llm = _CapUserLLM()
    syn = QASynthesizer(llm)
    await syn.synthesize_stream(_ctx(skill_id="chit-chat"), history=_HIST)
    assert "【对话历史】" in llm.last_user and "我喜欢吃西瓜" in llm.last_user
    assert llm.last_user.endswith("下单流程怎么走")


@pytest.mark.asyncio
async def test_chitchat_no_history_is_bare_question_backward_compat():
    llm = _CapUserLLM()
    syn = QASynthesizer(llm)
    await syn.synthesize(_ctx(skill_id="chit-chat"))
    assert llm.last_user == "下单流程怎么走"
    llm2 = _CapUserLLM()
    await QASynthesizer(llm2).synthesize_stream(_ctx(skill_id="chit-chat"))
    assert llm2.last_user == "下单流程怎么走"


class _CapBothLLM:
    def __init__(self):
        self.last_system = None
        self.last_user = None

    async def complete(self, *, system, user, **kw):
        self.last_system = system
        self.last_user = user
        return "ok"

    async def complete_stream(self, *, system, user, **kw):
        self.last_system = system
        self.last_user = user
        yield "ok"


@pytest.mark.asyncio
async def test_chitchat_composes_history_and_memory_block():
    llm = _CapBothLLM()
    syn = QASynthesizer(llm)
    await syn.synthesize(
        _ctx(skill_id="chit-chat"),
        history=_HIST,
        memory_block="用户偏好：只看支付域",
    )
    # 近轮原文进 user prompt
    assert "【对话历史】" in llm.last_user and "我喜欢吃西瓜" in llm.last_user
    # 旧轮/记忆经 memory_block 进 system prompt（§7/§20 两层并存）
    assert "用户偏好：只看支付域" in llm.last_system


# ───────── §21：会话压缩递归累积输入 ─────────

class _CapCompactLLM:
    """捕获 maybe_compact_session 喂给摘要器的 user 输入。"""
    def __init__(self, reply="更新后的摘要"):
        self._reply = reply
        self.last_user = None

    async def complete(self, *, system, user, **kw):
        self.last_user = user
        return self._reply


@pytest.mark.asyncio
async def test_compact_first_time_no_prior_summary_segment():
    # 首次压缩（sm=None）：输入只有【新增对话】，无【已有会话摘要】段
    msgs = [_FakeMsg(role="user", content="我喜欢吃哈密瓜")] + [
        _FakeMsg(role="assistant", content=f"a{i}") for i in range(5)
    ]
    llm = _CapCompactLLM()
    db = _FakeMemDB(session_row=None, msg_rows=msgs)
    await maybe_compact_session(db, llm, session_id="s1", every_n_messages=6)
    assert "【已有会话摘要】" not in llm.last_user
    assert "【新增对话】" in llm.last_user
    assert "我喜欢吃哈密瓜" in llm.last_user
    row = [o for o in db.added if isinstance(o, QASessionMemory)][0]
    assert row.working_summary == "更新后的摘要" and row.turn_count == 6


@pytest.mark.asyncio
async def test_compact_recursive_folds_prior_summary_and_only_new_msgs():
    # 二次压缩：sm 已有 working_summary（含"哈密瓜"）+ turn_count=4（水位线）
    # 10 条消息：前 4 条是"老原始消息"，messages[4:] 是 6 条新增；
    # 增量 6 == every_n_messages(6) → 正常触发，无需 force（与 force 语义解耦）
    sm = QASessionMemory(session_id="s1",
                          working_summary="用户最早喜欢哈密瓜", turn_count=4)
    msgs = [_FakeMsg(role="user", content=f"OLD-{i}") for i in range(4)] + [
        _FakeMsg(role="user", content="现在喜欢西瓜"),
        _FakeMsg(role="assistant", content="好的西瓜"),
        _FakeMsg(role="user", content="夏天到了"),
        _FakeMsg(role="assistant", content="确实"),
        _FakeMsg(role="user", content="再聊聊"),
        _FakeMsg(role="assistant", content="嗯"),
    ]
    llm = _CapCompactLLM()
    db = _FakeMemDB(session_row=sm, msg_rows=msgs)
    await maybe_compact_session(db, llm, session_id="s1", every_n_messages=6)
    u = llm.last_user
    # 递归：含【已有会话摘要】+ 旧摘要内容（哈密瓜经此被保留，非靠老原始消息）
    assert "【已有会话摘要】\n用户最早喜欢哈密瓜" in u
    # 【新增对话】只含 messages[prev=4:]，不重复老原始消息
    assert "【新增对话】" in u
    assert "现在喜欢西瓜" in u and "夏天到了" in u
    assert "OLD-0" not in u and "OLD-3" not in u
    assert sm.working_summary == "更新后的摘要" and sm.turn_count == 10


@pytest.mark.asyncio
async def test_compact_existing_fixed_fake_still_works_regression():
    # 既有风格 fake（固定返回、与输入无关）仍正常 upsert（不回归）
    sm = QASessionMemory(session_id="s1", working_summary="old", turn_count=0)
    msgs = [_FakeMsg() for _ in range(6)]
    db = _FakeMemDB(session_row=sm, msg_rows=msgs)
    await maybe_compact_session(db, _FakeMemLLM(), session_id="s1",
                                every_n_messages=6)
    assert sm.working_summary  # 被更新
    assert sm.turn_count == 6
    assert db.committed is True


# ───────── §22.3：detect_explicit_memory 前缀或句尾后缀 ─────────

def test_detect_prefix_still_works_no_regression():
    assert detect_explicit_memory("记住我喜欢简短回答") == "我喜欢简短回答"
    assert detect_explicit_memory("请记住：我用 Java") == "我用 Java"


def test_detect_suffix_trailing_trigger():
    # 用户实测说法：触发词在句尾
    assert detect_explicit_memory("我改名叫李龙飞 请记住") == "我改名叫李龙飞"
    assert detect_explicit_memory("以后叫我李龙飞 记住") == "以后叫我李龙飞"


def test_detect_suffix_strips_trailing_punctuation():
    assert detect_explicit_memory("我改名叫李龙飞，请记住。") == "我改名叫李龙飞"
    assert detect_explicit_memory("我喜欢简短回答！记一下！") == "我喜欢简短回答"


def test_detect_prefix_priority_when_both():
    # 前缀优先：句首是触发词则按前缀剥
    assert detect_explicit_memory("记住我改名叫李龙飞 请记住") == "我改名叫李龙飞"


def test_detect_none_and_empty():
    assert detect_explicit_memory("今天天气不错") is None
    assert detect_explicit_memory("请记住") is None        # 只有触发词无内容
    assert detect_explicit_memory("请记住。") is None       # 触发词+标点无内容
    assert detect_explicit_memory("") is None
    assert detect_explicit_memory("我不需要你记住这个东西") is None  # 触发词在中间不算


# ───────── §22.3 review 收尾：endswith 最长优先 + content==trigger 不过剥 + ；; ─────────

def test_detect_suffix_bangwo_jizhu_longest_first():
    # Critical：「帮我记住」是「记住」的超后缀；顺序匹配会误剥成「…帮我」
    assert detect_explicit_memory("我喜欢直接回答 帮我记住") == "我喜欢直接回答"
    assert detect_explicit_memory("以后回答都简短点，帮我记住。") == "以后回答都简短点"


def test_detect_prefix_bangwo_jizhu():
    assert detect_explicit_memory("帮我记住我用 Python") == "我用 Python"


def test_detect_content_equals_trigger_not_overstripped():
    # Important：内容本身就是触发词，不能被尾部触发词剥空（回归旧行为）
    assert detect_explicit_memory("请记住记住") == "记住"
    assert detect_explicit_memory("记住 记住") == "记住"


def test_detect_suffix_semicolon_punct():
    # Minor：；; 也应作尾部标点剥掉
    assert detect_explicit_memory("我叫李龙飞；请记住；") == "我叫李龙飞"
    assert detect_explicit_memory("我用 Java; 记住;") == "我用 Java"


# ───────── §22.4：parse_user_memory_intent 解析 + 兜底 ─────────


class _JsonLLM:
    def __init__(self, reply): self._reply = reply
    async def complete(self, *, system, user, **kw): return self._reply


class _RaiseLLM:
    async def complete(self, *, system, user, **kw): raise RuntimeError("llm down")


@pytest.mark.asyncio
async def test_parse_valid_json():
    llm = _JsonLLM('{"tier":"user","kind":"identity",'
                    '"content":"用户的名字是李龙飞","supersedes_kind":"identity"}')
    r = await parse_user_memory_intent(llm, "我改名叫李龙飞")
    assert r == {"tier": "user", "kind": "identity",
                 "content": "用户的名字是李龙飞", "supersedes_kind": "identity"}


@pytest.mark.asyncio
async def test_parse_strips_code_fence():
    llm = _JsonLLM('```json\n{"tier":"user","kind":"preference",'
                   '"content":"用户偏好简短回答","supersedes_kind":null}\n```')
    r = await parse_user_memory_intent(llm, "我喜欢简短回答")
    assert r["kind"] == "preference" and r["supersedes_kind"] is None


@pytest.mark.asyncio
async def test_parse_skip():
    llm = _JsonLLM('{"tier":"skip","kind":"preference","content":"x","supersedes_kind":null}')
    r = await parse_user_memory_intent(llm, "嗯嗯")
    assert r["tier"] == "skip"


@pytest.mark.asyncio
async def test_parse_invalid_json_falls_back():
    # 非 JSON → 兜底：原样 content 当 preference，不丢意图
    llm = _JsonLLM("好的我记住了")
    r = await parse_user_memory_intent(llm, "我喜欢简短回答")
    assert r == {"tier": "user", "kind": "preference",
                 "content": "我喜欢简短回答", "supersedes_kind": None}


@pytest.mark.asyncio
async def test_parse_llm_raises_falls_back():
    r = await parse_user_memory_intent(_RaiseLLM(), "我用 Java")
    assert r == {"tier": "user", "kind": "preference",
                 "content": "我用 Java", "supersedes_kind": None}


@pytest.mark.asyncio
async def test_parse_bad_enum_or_missing_keys_falls_back():
    llm = _JsonLLM('{"tier":"weird","kind":"nope"}')   # 非法枚举/缺字段
    r = await parse_user_memory_intent(llm, "我用 Java")
    assert r == {"tier": "user", "kind": "preference",
                 "content": "我用 Java", "supersedes_kind": None}


@pytest.mark.asyncio
async def test_parse_non_dict_json_falls_back():
    # 合法 JSON 但非 dict（list / 裸字符串 / 数字）→ 兜底
    for reply in ('[1, 2, 3]', '"juststr"', '42'):
        r = await parse_user_memory_intent(_JsonLLM(reply), "我用 Go")
        assert r == {"tier": "user", "kind": "preference",
                     "content": "我用 Go", "supersedes_kind": None}


@pytest.mark.asyncio
async def test_parse_weird_supersedes_kind_coerced_none():
    llm = _JsonLLM('{"tier":"user","kind":"preference",'
                   '"content":"用户偏好简短","supersedes_kind":"weird"}')
    r = await parse_user_memory_intent(llm, "我喜欢简短")
    assert r["supersedes_kind"] is None and r["kind"] == "preference"


@pytest.mark.asyncio
async def test_parse_whitespace_content_falls_back():
    llm = _JsonLLM('{"tier":"user","kind":"preference",'
                   '"content":"   ","supersedes_kind":null}')
    r = await parse_user_memory_intent(llm, "原始话")
    assert r == {"tier": "user", "kind": "preference",
                 "content": "原始话", "supersedes_kind": None}


# ───────── §22.5：write_explicit_memory identity 单例取代 / preference 累加 ─────────

def _um(kind, content, status="active"):
    return QAUserMemory(user_id=7, kind=kind, content=content,
                        source="explicit", source_session_id="s0", status=status)


@pytest.mark.asyncio
async def test_write_default_still_preference_no_regression():
    # 既有调用（不传 kind）行为不变：追加一条 preference
    db = _FakeMemDB()
    await write_explicit_memory(db, user_id=7, session_id="s1", content="我用 Java")
    assert len(db.added) == 1
    assert db.added[0].kind == "preference" and db.added[0].status == "active"


@pytest.mark.asyncio
async def test_write_identity_supersedes_old_identity_only():
    old_id = _um("identity", "用户的名字是王山河")
    pref = _um("preference", "用户偏好简短回答")
    db = _FakeMemDB(user_rows=[old_id, pref])
    await write_explicit_memory(
        db, user_id=7, session_id="s2", content="用户的名字是李龙飞",
        kind="identity", supersedes_kind="identity",
    )
    assert old_id.status == "archived"          # 旧 identity 归档
    assert pref.status == "active"              # preference 不受影响
    new_rows = [o for o in db.added if isinstance(o, QAUserMemory)]
    assert len(new_rows) == 1
    assert new_rows[0].kind == "identity"
    assert new_rows[0].content == "用户的名字是李龙飞"
    assert new_rows[0].status == "active"
    assert db.committed is True


@pytest.mark.asyncio
async def test_write_preference_appends_no_archive():
    old_pref = _um("preference", "用户偏好简短回答")
    db = _FakeMemDB(user_rows=[old_pref])
    await write_explicit_memory(
        db, user_id=7, session_id="s3", content="用户只看支付域",
        kind="preference", supersedes_kind=None,
    )
    assert old_pref.status == "active"          # 旧 preference 不归档（累加）
    assert len([o for o in db.added if isinstance(o, QAUserMemory)]) == 1


@pytest.mark.asyncio
async def test_recall_only_current_identity_after_supersede():
    # 取代后召回只剩李龙飞（模拟旧行已 archived，DB 过滤后只返回 active）
    db = _FakeMemDB(user_rows=[_um("identity", "用户的名字是李龙飞")])
    block = await recall_memory_block(db, user_id=7, session_id="sX")
    assert "李龙飞" in block and "王山河" not in block


# ───────── §22.5 review 收尾：锁 SQL WHERE + 零/多旧 identity ─────────

class _CapSelectDB(_FakeMemDB):
    """记录传给 execute 的 QAUserMemory 查询语句，用于断言 WHERE 子句存在
    （_FakeMemDB 本身不套 WHERE；这层捕获让 WHERE 回归可被测试发现）。"""
    def __init__(self, user_rows=None):
        super().__init__(user_rows=user_rows)
        self.last_user_select = None

    async def execute(self, stmt):
        ent = stmt.column_descriptions[0]["entity"]
        if ent is QAUserMemory:
            self.last_user_select = stmt
        return await super().execute(stmt)


@pytest.mark.asyncio
async def test_write_identity_select_has_where_filter():
    # 锁生产 SQL：身份写必须按 user_id + kind='identity' + status='active' 过滤；
    # 若 WHERE 被误删，此测试失败（_FakeMemDB 的 Python guard 兜不住这条断言）
    old_id = _um("identity", "用户的名字是王山河")
    db = _CapSelectDB(user_rows=[old_id])
    await write_explicit_memory(
        db, user_id=7, session_id="s9", content="用户的名字是李龙飞",
        kind="identity", supersedes_kind="identity",
    )
    assert db.last_user_select is not None
    sql = str(db.last_user_select).lower()
    assert "where" in sql
    assert "user_id" in sql and "kind" in sql and "status" in sql
    assert old_id.status == "archived"


@pytest.mark.asyncio
async def test_write_identity_no_prior_just_inserts():
    db = _FakeMemDB(user_rows=[])
    await write_explicit_memory(
        db, user_id=7, session_id="s10", content="用户的名字是李龙飞",
        kind="identity", supersedes_kind="identity",
    )
    rows = [o for o in db.added if isinstance(o, QAUserMemory)]
    assert len(rows) == 1 and rows[0].kind == "identity"
    assert rows[0].content == "用户的名字是李龙飞" and rows[0].status == "active"
    assert db.committed is True


@pytest.mark.asyncio
async def test_write_identity_archives_all_prior_actives():
    # 生产 bug 场景：历史遗留 2 条 active identity → 新身份写必须全部归档
    a = _um("identity", "名字A")
    b = _um("identity", "名字B")
    db = _FakeMemDB(user_rows=[a, b])
    await write_explicit_memory(
        db, user_id=7, session_id="s11", content="用户的名字是李龙飞",
        kind="identity", supersedes_kind="identity",
    )
    assert a.status == "archived" and b.status == "archived"
    new = [o for o in db.added if isinstance(o, QAUserMemory)]
    assert len(new) == 1 and new[0].kind == "identity" and new[0].status == "active"
