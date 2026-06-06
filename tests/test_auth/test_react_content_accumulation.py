"""ReAct 多轮文本路由。

历史（RV-B1）：raw_output 曾每轮覆盖 → 丢第1轮正文；当时改为"累加所有轮"修内容丢失。
现态（旁白泄漏修复）：累加所有轮把"让我查…"这类**过程旁白**也塞进了正文。故收敛为——
  - **流式 `synthesize_stream`（用户实际走的 SSE 路径）**：工具轮的文本=过程旁白 → on_thinking（灰字思考），
    只有最终答案轮进正文 raw_output。见 test_synthesize_stream_routes_pretool_text_to_thinking。
  - **非流式 `synthesize`**：无 on_thinking 通道，仍累加所有轮进 raw_output（不丢、但也不分流）——非用户路径。
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
async def test_synthesize_stream_routes_pretool_text_to_thinking() -> None:
    """流式 synthesize_stream（真流路径，旁白泄漏修复后）：
    工具轮的过程旁白 → on_thinking（灰字思考），**不进** raw_output；只有最终答案轮进 raw_output。
    """
    thinks: list[str] = []                       # 收集灰字思考过程

    async def on_thinking(t: str) -> None:
        thinks.append(t)

    llm = _FakeStreamLLM([
        ("先看调用关系：", [ToolCall(id="c1", name="ke_callees", arguments={"entity_id": "method//M1"})]),
        ("这是最终业务流程详解。", []),
    ])
    synth = ReActSynthesizer(llm_provider=llm, tool_registry=_registry(), max_iterations=3)
    r = await synth.synthesize_stream(
        _ctx(), on_token=AsyncMock(), on_thinking=on_thinking, on_tool_call=AsyncMock(),
    )
    # 第1轮（工具轮）旁白 → 灰字思考过程，绝不进正文
    assert "先看调用关系" in "".join(thinks)
    assert "先看调用关系" not in r.raw_output
    # 第2轮（最终答案轮）进正文
    assert "这是最终业务流程详解" in r.raw_output
