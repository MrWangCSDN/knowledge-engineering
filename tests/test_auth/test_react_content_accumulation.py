"""ReAct 多轮文本累加：修"内容丢失"——agent 调工具前那一轮的正文不能被最后一轮覆盖。

线上现象：用户问订单题，agent 第1轮说"先看调用关系"+调 render_call_graph，第2轮给详解；
但 raw_output 每轮覆盖 → 最终只剩第2轮，第1轮正文丢失（"调用完成后内容丢失"）。
"""
from unittest.mock import AsyncMock, MagicMock
import pytest

from src.service.qa_engine.react_synthesizer import ReActSynthesizer
from src.service.qa_engine.llm_types import LLMToolResponse, ToolCall, StreamTextDelta
from src.service.qa_engine.retriever import RetrievedContext
from src.service.qa_engine.tools.base import ToolRegistry
from src.service.qa_engine.tools.ke_callees import build_ke_callees_tool


def _ctx():
    return RetrievedContext(question="q", project_id="p",
                            entry_candidates=[{"entity_id": "method//M1", "summary_text": "x", "level": "api"}])


def _registry():
    g = MagicMock(); g.successors.return_value = ["method//B"]
    r = ToolRegistry(); r.register(build_ke_callees_tool(g))
    return r


@pytest.mark.asyncio
async def test_synthesize_accumulates_pretool_text() -> None:
    """非流式 synthesize：第1轮正文 + 第2轮正文都进 raw_output（不丢第1轮）。"""
    round1 = LLMToolResponse(content="先看调用关系：",
                             tool_calls=[ToolCall(id="c1", name="ke_callees", arguments={"entity_id": "method//M1"})])
    round2 = LLMToolResponse(content="这是最终业务流程详解。", tool_calls=[])
    llm = AsyncMock()
    llm.complete_with_tools = AsyncMock(side_effect=[round1, round2])
    synth = ReActSynthesizer(llm_provider=llm, tool_registry=_registry(), max_iterations=3)
    r = await synth.synthesize(_ctx())
    assert "先看调用关系" in r.raw_output      # 第1轮正文不丢
    assert "这是最终业务流程详解" in r.raw_output  # 第2轮正文也在


class _FakeStreamLLM:
    """real-stream provider：每轮 yield 文本 delta + tool_calls（驱动 synthesize_stream 真流路径）。"""
    def __init__(self, rounds):
        self._rounds = rounds   # [(text, [ToolCall,...]), ...]
        self._i = 0

    async def complete_stream_with_tools(self, messages, tools):
        text, tcs = self._rounds[self._i]
        self._i += 1
        if text:
            yield StreamTextDelta(text=text)
        for tc in tcs:
            yield tc


@pytest.mark.asyncio
async def test_synthesize_stream_accumulates_pretool_text() -> None:
    """流式 synthesize_stream（真流路径）：两轮正文都进 raw_output。"""
    llm = _FakeStreamLLM([
        ("先看调用关系：", [ToolCall(id="c1", name="ke_callees", arguments={"entity_id": "method//M1"})]),
        ("这是最终业务流程详解。", []),
    ])
    synth = ReActSynthesizer(llm_provider=llm, tool_registry=_registry(), max_iterations=3)
    r = await synth.synthesize_stream(_ctx(), on_token=AsyncMock(), on_tool_call=AsyncMock())
    assert "先看调用关系" in r.raw_output
    assert "这是最终业务流程详解" in r.raw_output
