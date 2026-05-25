"""sse_emitter 把 todo_write 工具调用转成 `todo` SSE 事件（设计 §8）。

真集成测试：用 MagicMock(spec=ReActSynthesizer)（确保 isinstance 命中 → on_tool_call 接线）
+ 假 synthesize_stream（触发一次 todo_write 的 on_tool_call），驱动 stream_qa_answer，
断言输出里出现 `todo` 事件且带 items、且不为 todo_write 额外发普通 tool_call。
"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.service.qa_engine.sse_emitter import stream_qa_answer
from src.service.qa_engine.react_synthesizer import ReActSynthesizer
from src.service.qa_engine.retriever import RetrievedContext
from src.service.qa_engine.synthesizer import SynthesizedAnswer
from src.service.qa_engine.llm_types import ToolCall


def _build_mock_retriever():
    r = MagicMock()
    r.retrieve = AsyncMock(return_value=RetrievedContext(question="q", project_id="p"))
    return r


def _data_of(events: list[str], etype: str) -> list[dict]:
    """从 SSE event 字符串列表里挑出某类型事件的 data dict。"""
    out: list[dict] = []
    for e in events:
        if e.startswith(f"event: {etype}\n"):
            line = next(l for l in e.split("\n") if l.startswith("data: "))
            out.append(json.loads(line[len("data: "):]))
    return out


@pytest.mark.asyncio
async def test_stream_emits_todo_event_on_todo_write_call():
    items = [
        {"content": "分析订单域入口", "status": "in_progress"},
        {"content": "画调用链", "status": "pending"},
    ]

    synth = MagicMock(spec=ReActSynthesizer)

    async def fake_stream(ctx, history=None, on_token=None, on_thinking=None,
                          on_tool_call=None, memory_block=None, **kwargs):
        if on_tool_call:
            call = ToolCall(id="t1", name="todo_write", arguments={"items": items})
            await on_tool_call("starting", call)
            await on_tool_call("complete", call, {"items": items, "count": 2})
        if on_token:
            await on_token("答案")
        return SynthesizedAnswer(
            sections=[{"type": "overview", "title": "x", "content": "y", "references": []}],
            token_usage=1,
        )

    synth.synthesize_stream = AsyncMock(side_effect=fake_stream)

    events: list[str] = []
    async for c in stream_qa_answer(
        question="q", project_id="p", session_id="s",
        retriever=_build_mock_retriever(), synthesizer=synth,
    ):
        events.append(c)

    # 出现且只出现 1 个 todo 事件，带完整 items
    todos = _data_of(events, "todo")
    assert len(todos) == 1
    assert todos[0]["items"] == items

    # todo_write 不应再被当普通 tool_call 事件发
    tool_calls = _data_of(events, "tool_call")
    assert all(tc.get("name") != "todo_write" for tc in tool_calls)
