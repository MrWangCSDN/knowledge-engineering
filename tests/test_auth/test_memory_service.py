"""记忆注入 + 服务逻辑测试（Fake DB/LLM，不起真 engine）。
设计：[[记忆系统-设计]] §6 §7
"""
import pytest

from src.service.qa_engine.synthesizer import QASynthesizer
from src.service.qa_engine.retriever import RetrievedContext
from src.service.qa_engine.sse_emitter import stream_qa_answer
from src.service.memory.service import (
    maybe_compact_session,
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

    async def _on_memory(answer: str):
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


