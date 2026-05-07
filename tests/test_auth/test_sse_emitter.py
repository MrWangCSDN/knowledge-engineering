"""验证 SSE emitter：异步生成器 + 事件序列。"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.service.qa_engine.sse_emitter import format_sse, stream_qa_answer
from src.service.qa_engine.retriever import RetrievedContext
from src.service.qa_engine.synthesizer import SynthesizedAnswer


# ───────── format_sse ─────────

def test_format_sse_basic():
    s = format_sse("meta", {"k": "v"})
    assert s.startswith("event: meta\n")
    assert "data: " in s
    assert s.endswith("\n\n")  # SSE 协议要求双换行结尾


def test_format_sse_chinese_no_escape():
    """中文不能被 ASCII escape（前端解析会乱码）。"""
    s = format_sse("content", {"delta": "存款开户"})
    assert "存款开户" in s
    assert "\\u" not in s  # 不应有 unicode escape


def test_format_sse_json_safe():
    """data 字段必须是 JSON 一行（避免被 SSE 解析器吃掉）。"""
    s = format_sse("step", {"phase": "x", "desc": "y"})
    # data: 行不应含原始换行
    data_line = next(l for l in s.split("\n") if l.startswith("data: "))
    assert "\n" not in data_line[6:]


# ───────── stream_qa_answer ─────────

def _build_mock_retriever():
    r = MagicMock()
    r.retrieve = AsyncMock(return_value=RetrievedContext(
        question="x", project_id="p",
        entry_candidates=[{"entity_id": "method://m1"}],
    ))
    return r


def _build_mock_synthesizer(sections=None):
    s = MagicMock()
    sections = sections or [
        {"type": "overview", "title": "业务概述", "content": "概述内容", "references": []},
        {"type": "entry_point", "title": "入口方法", "content": "method content",
         "references": [{"entity_id": "method://m1", "display_text": "M1", "kind": "method"}]},
    ]
    s.synthesize = AsyncMock(return_value=SynthesizedAnswer(
        sections=sections,
        token_usage=120,
        cost_yuan=0.05,
    ))
    return s


@pytest.mark.asyncio
async def test_stream_emits_meta_first():
    """第一个事件必须是 meta（前端用它拿 session_id）。"""
    events = []
    async for chunk in stream_qa_answer(
        question="x", project_id="p", session_id="sess_1",
        retriever=_build_mock_retriever(),
        synthesizer=_build_mock_synthesizer(),
    ):
        events.append(chunk)

    first = events[0]
    assert first.startswith("event: meta\n")


@pytest.mark.asyncio
async def test_stream_emits_done_last():
    events = []
    async for chunk in stream_qa_answer(
        question="x", project_id="p", session_id="sess_1",
        retriever=_build_mock_retriever(),
        synthesizer=_build_mock_synthesizer(),
    ):
        events.append(chunk)

    last = events[-1]
    assert last.startswith("event: done\n")


@pytest.mark.asyncio
async def test_stream_emits_step_events_during_processing():
    events = []
    async for chunk in stream_qa_answer(
        question="x", project_id="p", session_id="sess_1",
        retriever=_build_mock_retriever(),
        synthesizer=_build_mock_synthesizer(),
    ):
        events.append(chunk)

    event_types = [e.split("\n", 1)[0].replace("event: ", "") for e in events]
    # 至少出现这些类型
    assert "meta" in event_types
    assert "step" in event_types
    assert "section_start" in event_types
    assert "content" in event_types
    assert "section_done" in event_types
    assert "done" in event_types


@pytest.mark.asyncio
async def test_stream_one_section_emits_3_events():
    """每段产生 3 个事件：section_start + content + section_done。"""
    events = []
    async for chunk in stream_qa_answer(
        question="x", project_id="p", session_id="sess_1",
        retriever=_build_mock_retriever(),
        # 只 1 段
        synthesizer=_build_mock_synthesizer(sections=[
            {"type": "overview", "title": "x", "content": "y", "references": []},
        ]),
    ):
        events.append(chunk)

    event_types = [e.split("\n", 1)[0].replace("event: ", "") for e in events]
    assert event_types.count("section_start") == 1
    assert event_types.count("content") == 1
    assert event_types.count("section_done") == 1


@pytest.mark.asyncio
async def test_stream_section_done_carries_references():
    """section_done 事件必须带 references（前端用它渲染链接）。"""
    refs = [{"entity_id": "method://x", "display_text": "X", "kind": "method"}]
    events = []
    async for chunk in stream_qa_answer(
        question="x", project_id="p", session_id="sess_1",
        retriever=_build_mock_retriever(),
        synthesizer=_build_mock_synthesizer(sections=[
            {"type": "overview", "title": "x", "content": "y", "references": refs},
        ]),
    ):
        events.append(chunk)

    sd = next(e for e in events if e.startswith("event: section_done\n"))
    assert "method://x" in sd


@pytest.mark.asyncio
async def test_stream_done_carries_session_and_metrics():
    events = []
    async for chunk in stream_qa_answer(
        question="x", project_id="p", session_id="sess_abc",
        retriever=_build_mock_retriever(),
        synthesizer=_build_mock_synthesizer(),
    ):
        events.append(chunk)

    done = events[-1]
    assert "sess_abc" in done
    assert "total_tokens" in done
    assert "latency_ms" in done


@pytest.mark.asyncio
async def test_stream_propagates_question_to_retriever():
    retriever = _build_mock_retriever()
    synth = _build_mock_synthesizer()
    async for _ in stream_qa_answer(
        question="存款开户", project_id="proj-x", session_id="s1",
        retriever=retriever, synthesizer=synth,
    ):
        pass

    retriever.retrieve.assert_called_once()
    call_kwargs = retriever.retrieve.call_args.kwargs
    assert call_kwargs["question"] == "存款开户"
    assert call_kwargs["project_id"] == "proj-x"
