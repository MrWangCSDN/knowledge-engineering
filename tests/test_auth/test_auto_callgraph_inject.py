"""A2 确定性调用图注入：门控 + 合成事件构建 + sse 注入。设计 [[业务问答-确定性调用图注入-A2设计]]。

根治 agent 路径 render_call_graph 触发受 LLM 波动（有时不出图）：架构题(有调用边)由系统
确定性构图、注入答案开头，不靠 agent 自觉调工具。
"""
from src.service.qa_engine.sse_emitter import _should_auto_render, build_auto_call_graph_event
from src.service.qa_engine.retriever import RetrievedContext


def _arch_ctx():
    """架构题 ctx：skill=architecture + 有调用边 + 一条 2b 中文解读。"""
    return RetrievedContext(
        question="用户提交订单怎么生成订单", project_id="p", skill_id="architecture",
        call_edges_by_entry={"OmsPortalOrderServiceImpl::generateOrder": [
            ("OmsPortalOrderServiceImpl::generateOrder", "OmsPortalOrderServiceImpl::lockStock")]},
        callchain_node_summaries={"OmsPortalOrderServiceImpl::generateOrder": "生成订单主流程"},
    )


def test_gate_true_for_architecture_with_edges():
    assert _should_auto_render(_arch_ctx()) is True


def test_gate_false_for_chitchat():
    c = _arch_ctx(); c.skill_id = "chit-chat"
    assert _should_auto_render(c) is False


def test_gate_false_for_empty_edges():
    c = _arch_ctx(); c.call_edges_by_entry = {}
    assert _should_auto_render(c) is False


def test_build_event_when_gated():
    ev = build_auto_call_graph_event(_arch_ctx())
    assert ev is not None
    assert ev["name"] == "render_call_graph" and ev["phase"] == "complete" and ev["at"] == 0
    data = ev["render"]["data"]
    assert ev["render"]["kind"] == "call_graph" and len(data["nodes"]) >= 1
    # 节点中英双语（B3）：有 method 字段
    assert any(n.get("method") for n in data["nodes"])


def test_build_event_none_when_not_gated():
    c = _arch_ctx(); c.skill_id = "chit-chat"
    assert build_auto_call_graph_event(c) is None


def test_build_event_fail_soft_on_bad_ctx():
    class _Bad:  # 无 skill_id / call_edges_by_entry 属性 → 不崩、返 None
        pass
    assert build_auto_call_graph_event(_Bad()) is None


# ─── sse_emitter 注入接线 ────────────────────────────────────────────────────
import re
import json as _json
import pytest
from unittest.mock import AsyncMock, MagicMock
from src.service.qa_engine.sse_emitter import stream_qa_answer
from src.service.qa_engine.react_synthesizer import ReActSynthesizer
from src.service.qa_engine.synthesizer import SynthesizedAnswer


def _mock_retriever(ctx):
    r = MagicMock(); r.retrieve = AsyncMock(return_value=ctx)
    return r


def _mock_react_synth():
    synth = MagicMock(spec=ReActSynthesizer)
    final = SynthesizedAnswer(sections=[{"type": "overview", "title": "x", "content": "y", "references": []}],
                              token_usage=10, cost_yuan=0.0)
    async def fake_stream(ctx, history=None, on_token=None, on_tool_call=None, memory_block=None, **kw):
        if on_token:
            await on_token("订单生成流程：")
        return final
    synth.synthesize_stream = AsyncMock(side_effect=fake_stream)
    return synth


@pytest.mark.asyncio
async def test_sse_injects_auto_callgraph_for_architecture():
    """架构题(有边) → SSE body 含 tool_call render 事件且 at:0（确定性注入主图）。"""
    body = "".join([c async for c in stream_qa_answer(
        question="q", project_id="p", session_id="s",
        retriever=_mock_retriever(_arch_ctx()), synthesizer=_mock_react_synth())])
    assert "event: tool_call" in body
    tc = re.findall(r"event: tool_call\ndata: (\{.*\})", body)
    assert any(_json.loads(b).get("name") == "render_call_graph" and _json.loads(b).get("at") == 0
               and _json.loads(b).get("render") for b in tc)


@pytest.mark.asyncio
async def test_sse_no_inject_for_chitchat():
    """chit-chat → 不注入自动图。"""
    c = _arch_ctx(); c.skill_id = "chit-chat"
    body = "".join([x async for x in stream_qa_answer(
        question="q", project_id="p", session_id="s",
        retriever=_mock_retriever(c), synthesizer=_mock_react_synth())])
    tc = re.findall(r"event: tool_call\ndata: (\{.*\})", body)
    assert not any(_json.loads(b).get("id") == "auto_cg" for b in tc)
