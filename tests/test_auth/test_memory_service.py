"""记忆注入 + 服务逻辑测试（Fake DB/LLM，不起真 engine）。
设计：[[记忆系统-设计]] §6 §7
"""
import pytest

from src.service.qa_engine.synthesizer import QASynthesizer
from src.service.qa_engine.retriever import RetrievedContext
from src.service.qa_engine.sse_emitter import stream_qa_answer
from src.service.memory.service import _extract_focus_entity_ids


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
        called["answer"] = answer

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
    # 锁定 answer_text 构造契约：sse_emitter 必须把 answer.sections 拼接传给 on_memory
    # （单 section "c" → answer_text 应为 "c"）
    assert called["answer"] == "c"


# ───────── memory.service 逻辑（Fake DB/LLM）─────────

# ───────── 会话级 focus_entity_ids 抽取（spec §17）─────────


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



