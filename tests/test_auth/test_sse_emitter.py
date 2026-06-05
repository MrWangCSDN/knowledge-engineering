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
    # spec=['synthesize']：限制只能访问这一个属性；访问其他属性（如 synthesize_stream）会抛 AttributeError
    # 这样 sse_emitter 里的 hasattr 检测能正确判定"不支持流式"
    s = MagicMock(spec=['synthesize'])
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


@pytest.mark.asyncio
async def test_stream_omits_skill_id_when_no_router() -> None:
    """没传 router 时，skill_id 不应出现在 retriever.retrieve 调用里（向后兼容）。"""
    retriever = _build_mock_retriever()
    synth = _build_mock_synthesizer()

    async for _ in stream_qa_answer(
        question="x", project_id="p", session_id="s",
        retriever=retriever, synthesizer=synth,  # router 没传
    ):
        pass

    call_kwargs = retriever.retrieve.call_args.kwargs
    # 没传 router → 不传 skill_id → retriever 用自己的默认值 "architecture"
    assert "skill_id" not in call_kwargs


# ───────── v1.6 token streaming ─────────


@pytest.mark.asyncio
async def test_stream_emits_token_events_when_synthesizer_supports_stream() -> None:
    """synthesizer 实现了 synthesize_stream 时，sse_emitter 应该：
       1. 优先调 synthesize_stream（而非 synthesize）
       2. 每个 token 都 emit 一个 `event: token` SSE 事件
       3. 流末仍 emit 标准 section_start / content / section_done / done
    """
    retriever = _build_mock_retriever()

    # 准备一个支持 stream 的 mock synthesizer
    # synthesize_stream(ctx, history=, on_token=) → 调 on_token 几次后返回 SynthesizedAnswer
    synth = MagicMock()

    sections = [{"type": "overview", "title": "概述", "content": "答案内容",
                 "references": []}]
    final_answer = SynthesizedAnswer(sections=sections, token_usage=10, cost_yuan=0.01)

    async def fake_stream(ctx, history=None, on_token=None, memory_block=None, **kwargs):
        # 模拟 4 个 chunk
        # memory_block/**kwargs：吸收 P2② 起 stream_qa_answer 透传的新 kwarg
        # （memory_block 等），防 fake 签名落后于真实 synthesize_stream 再致 TypeError
        for chunk in ["你好", "，", "世界", "！"]:
            if on_token:
                await on_token(chunk)
        return final_answer

    synth.synthesize_stream = AsyncMock(side_effect=fake_stream)
    # synthesize 也存在，但不应该被调用
    synth.synthesize = AsyncMock(return_value=final_answer)

    events: list[str] = []
    async for chunk in stream_qa_answer(
        question="x", project_id="p", session_id="s",
        retriever=retriever, synthesizer=synth,
    ):
        events.append(chunk)

    body = "".join(events)

    # 必须调 synthesize_stream，不是 synthesize
    assert synth.synthesize_stream.await_count == 1
    assert synth.synthesize.await_count == 0

    # v1.7：batching 后事件数小于等于 4（小 chunk 会被攒批）
    # 但完整内容仍能拼回："你好，世界！"
    token_count = body.count("event: token\n")
    assert 1 <= token_count <= 4, f"期望 1-4 token 事件，实际 {token_count}"
    # 拼接所有 token delta 后应该等于完整内容
    # 注意：content 事件也有 delta 字段，需要按 token 事件单独匹配
    import re
    # `event: token\ndata: {...}\n\n` 匹配每个 token 事件块
    token_blocks = re.findall(r"event: token\ndata: (\{[^}]*\})", body)
    deltas: list[str] = []
    import json as _json
    for blk in token_blocks:
        payload = _json.loads(blk)
        deltas.append(payload.get("delta", ""))
    assert "".join(deltas) == "你好，世界！"

    # 标准 section_start / content / section_done / done 仍存在
    assert "event: section_start" in body
    assert "event: content" in body
    assert "event: section_done" in body
    assert "event: done" in body


@pytest.mark.asyncio
async def test_stream_falls_back_to_non_stream_when_synthesizer_missing_method() -> None:
    """synthesizer 没实现 synthesize_stream 时（仅 synthesize），向后兼容走老路径，不报错。"""
    retriever = _build_mock_retriever()
    synth = _build_mock_synthesizer()  # 这个 mock 只有 synthesize，没有 synthesize_stream

    events: list[str] = []
    async for chunk in stream_qa_answer(
        question="x", project_id="p", session_id="s",
        retriever=retriever, synthesizer=synth,
    ):
        events.append(chunk)

    body = "".join(events)
    # 没有 token 事件
    assert "event: token" not in body
    # 但 section 事件和 done 都还在
    assert "event: section_start" in body
    assert "event: done" in body


# ───────── v1.7: ReAct streaming + tool_call 事件同流 ─────────


@pytest.mark.asyncio
async def test_stream_handles_react_synthesizer_with_token_and_tool_call_events() -> None:
    """ReActSynthesizer 实例：synthesize_stream 同时产 token 和 tool_call 回调，
    sse_emitter 都要正确 yield 出去。
    """
    from src.service.qa_engine.react_synthesizer import ReActSynthesizer
    from src.service.qa_engine.llm_types import ToolCall as _ToolCall

    retriever = _build_mock_retriever()

    sections = [{"type": "overview", "title": "x", "content": "y", "references": []}]
    final_answer = SynthesizedAnswer(sections=sections, token_usage=20, cost_yuan=0.0)

    # 用真 ReActSynthesizer 类做 spec，确保 isinstance 命中
    synth = MagicMock(spec=ReActSynthesizer)

    # synthesize_stream 模拟：先 emit 2 个 tool_call 事件，然后 5 个 token chunks
    async def fake_stream(ctx, history=None, on_token=None, on_tool_call=None,
                          memory_block=None, **kwargs):
        # memory_block/**kwargs：吸收 P2② 起 stream_qa_answer 透传的新 kwarg
        if on_tool_call:
            call = _ToolCall(id="c1", name="ke_callees", arguments={"entity_id": "M"})
            await on_tool_call("starting", call)
            await on_tool_call("complete", call, {"callees": ["B"]})
        if on_token:
            for ch in ["最终", "答案", "内容", "片", "段"]:
                await on_token(ch)
        return final_answer

    synth.synthesize_stream = AsyncMock(side_effect=fake_stream)

    events: list[str] = []
    async for chunk in stream_qa_answer(
        question="x", project_id="p", session_id="s",
        retriever=retriever, synthesizer=synth,
    ):
        events.append(chunk)

    body = "".join(events)

    # synthesize_stream 被调用
    assert synth.synthesize_stream.await_count == 1

    # tool_call 事件存在（starting + complete 各一个）
    assert "event: tool_call" in body
    assert body.count("event: tool_call\n") >= 2

    # token 事件存在；拼起来应该是完整 "最终答案内容片段"
    import re
    import json as _json
    token_blocks = re.findall(r"event: token\ndata: (\{[^}]*\})", body)
    deltas = [_json.loads(blk).get("delta", "") for blk in token_blocks]
    assert "".join(deltas) == "最终答案内容片段"

    # 标准 section + done 仍存在
    assert "event: section_start" in body
    assert "event: done" in body


@pytest.mark.asyncio
async def test_stream_tool_call_carries_at_offset_for_inline_render() -> None:
    """render 工具的 tool_call 事件带 at = 调工具时刻已流式字符数 → 前端据此内联插图（不甩末尾）。"""
    from src.service.qa_engine.react_synthesizer import ReActSynthesizer
    from src.service.qa_engine.llm_types import ToolCall as _ToolCall

    retriever = _build_mock_retriever()
    final_answer = SynthesizedAnswer(
        sections=[{"type": "overview", "title": "x", "content": "y", "references": []}],
        token_usage=10, cost_yuan=0.0)
    synth = MagicMock(spec=ReActSynthesizer)

    async def fake_stream(ctx, history=None, on_token=None, on_tool_call=None, memory_block=None, **kwargs):
        # 先流 6 个字 → offset 应为 6；再调 render 工具（at 锚定到这 6 个字之后）
        if on_token:
            await on_token("先看调用关系")
        if on_tool_call:
            call = _ToolCall(id="c1", name="render_call_graph", arguments={"entity_id": "M"})
            await on_tool_call("complete", call,
                               {"render": {"kind": "call_graph", "data": {"nodes": [], "edges": []}}, "summary": "图"})
        if on_token:
            await on_token("详解")
        return final_answer

    synth.synthesize_stream = AsyncMock(side_effect=fake_stream)

    events: list[str] = []
    async for chunk in stream_qa_answer(question="x", project_id="p", session_id="s",
                                        retriever=retriever, synthesizer=synth):
        events.append(chunk)
    body = "".join(events)

    import re
    import json as _json
    tc_blocks = re.findall(r"event: tool_call\ndata: (\{.*\})", body)
    assert tc_blocks, "应有 tool_call 事件"
    payload = _json.loads(tc_blocks[-1])
    assert payload.get("at") == 6                 # 内联锚点 = 调工具前已流式的 6 个字
    assert payload.get("render") is not None      # render 透传仍在


@pytest.mark.asyncio
async def test_stream_folds_render_into_on_complete_sections() -> None:
    """RV2-T3：agent 流式调 render_call_graph → on_complete 的 sections 按 at 折叠出 call_chain 段
    （持久化+有序，治"图跳末尾/reopen丢图"）。"""
    from src.service.qa_engine.react_synthesizer import ReActSynthesizer
    from src.service.qa_engine.llm_types import ToolCall as _ToolCall

    retriever = _build_mock_retriever()
    # 最终答案正文 = 流式吐的全文，便于 at=6 切片落在"先看调用关系"之后
    final_answer = SynthesizedAnswer(
        sections=[{"type": "overview", "title": "回答", "content": "先看调用关系详解"}],
        token_usage=10, cost_yuan=0.0)
    synth = MagicMock(spec=ReActSynthesizer)

    async def fake_stream(ctx, history=None, on_token=None, on_tool_call=None, memory_block=None, **kwargs):
        await on_token("先看调用关系")            # 6 字 → offset=6
        call = _ToolCall(id="c1", name="render_call_graph", arguments={"entity_id": "M"})
        await on_tool_call("complete", call,
                           {"render": {"kind": "call_graph", "data": {"nodes": [{"id": "n1"}], "edges": []}}, "summary": "图"})
        await on_token("详解")
        return final_answer

    synth.synthesize_stream = AsyncMock(side_effect=fake_stream)

    captured: dict = {}

    async def _on_complete(q, sections, meta):
        captured["sections"] = sections

    async for _ in stream_qa_answer(question="x", project_id="p", session_id="s",
                                    retriever=retriever, synthesizer=synth,
                                    on_complete=_on_complete):
        pass

    sections = captured["sections"]
    # 折叠成 [文本(先看调用关系), call_chain(图), 文本(详解)]，图在 at=6 处
    assert [s["type"] for s in sections] == ["overview", "call_chain", "overview"]
    assert sections[0]["content"] == "先看调用关系"
    assert sections[2]["content"] == "详解"
    assert '"n1"' in sections[1]["content"]            # 图数据进了 call_chain 段
    assert all(s.get("headerless") for s in sections)  # agent 自由输出无小节头
