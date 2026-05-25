"""验证 ReActSynthesizer：让 LLM 主动调 tool 的循环。

测试策略：
  - LLM 全部用 AsyncMock；不打真模型
  - ToolRegistry 用真实实现 + mock 后端，方便验证调用链
  - 用 `side_effect = [resp1, resp2, ...]` 模拟"LLM 多轮回答"
"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

# 待实现的 ReActSynthesizer
from src.service.qa_engine.react_synthesizer import ReActSynthesizer
from src.service.qa_engine.llm_types import LLMToolResponse, ToolCall
from src.service.qa_engine.retriever import RetrievedContext
from src.service.qa_engine.synthesizer import SynthesizedAnswer
from src.service.qa_engine.tools.base import Tool, ToolRegistry


# ───────── helpers ─────────


def _ok_answer_json() -> str:
    """合法的 6 段式 JSON LLM 输出。"""
    payload = {
        "sections": [
            {"type": "overview", "title": "业务概述",
             "content": "测试答案", "references": []},
        ]
    }
    return f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```"


def _make_ctx(question: str = "test 问题") -> RetrievedContext:
    return RetrievedContext(
        question=question,
        project_id="petclinic",
        entry_candidates=[{"entity_id": "method//M1", "summary_text": "x", "level": "api"}],
    )


def _empty_registry() -> ToolRegistry:
    """空 registry（没注册任何工具）—— 用于"LLM 不会调工具"场景。"""
    return ToolRegistry()


# ───────── RED 3: LLM 不调 tool，直接走完 ─────────


@pytest.mark.asyncio
async def test_react_returns_final_answer_when_llm_does_not_call_tools() -> None:
    """LLM 第一轮就给文本（tool_calls 空）→ ReAct 直接解析返回，不进入第二轮。"""
    llm = AsyncMock()
    # complete_with_tools 是 ReAct 用的新方法（区别于 QASynthesizer.complete）
    llm.complete_with_tools = AsyncMock(return_value=LLMToolResponse(
        content=_ok_answer_json(),
        tool_calls=[],
    ))

    synth = ReActSynthesizer(
        llm_provider=llm,
        tool_registry=_empty_registry(),
        max_iterations=3,
    )

    result = await synth.synthesize(_make_ctx())

    # 应该返回 SynthesizedAnswer
    assert isinstance(result, SynthesizedAnswer)
    # JSON 应该解析成功，有 overview 段
    assert len(result.sections) == 1
    assert result.sections[0]["type"] == "overview"

    # LLM 只被调用 1 次（没有进入第二轮）
    assert llm.complete_with_tools.await_count == 1


# ───────── RED 4: LLM 调 1 个 tool，下一轮给 final ─────────


@pytest.mark.asyncio
async def test_react_loops_when_llm_calls_tool() -> None:
    """两轮迭代：
       第 1 轮 LLM 要求 ke_callees(M1) → 后端执行 → 把结果喂回
       第 2 轮 LLM 看到 callees 数据后输出最终 JSON sections
    """
    # 第 1 轮：LLM 要调 ke_callees
    round1 = LLMToolResponse(
        content=None,  # 调工具时 content 通常为 None
        tool_calls=[
            ToolCall(
                id="call_001",
                name="ke_callees",
                arguments={"entity_id": "method//M1"},
            ),
        ],
    )
    # 第 2 轮：LLM 给最终答案
    round2 = LLMToolResponse(content=_ok_answer_json(), tool_calls=[])

    llm = AsyncMock()
    # `side_effect = [..., ...]`：mock 按顺序返回这些值（每次 await 返回下一个）
    llm.complete_with_tools = AsyncMock(side_effect=[round1, round2])

    # 注册一个真 ke_callees tool；底层 graph 用 mock
    graph_mock = MagicMock()
    graph_mock.successors.return_value = ["method//B", "method//C"]
    from src.service.qa_engine.tools.ke_callees import build_ke_callees_tool
    registry = ToolRegistry()
    registry.register(build_ke_callees_tool(graph_mock))

    synth = ReActSynthesizer(
        llm_provider=llm,
        tool_registry=registry,
        max_iterations=3,
    )
    result = await synth.synthesize(_make_ctx())

    # LLM 被调用 2 次：第 1 轮决定调 tool，第 2 轮看到 tool 结果给最终答案
    assert llm.complete_with_tools.await_count == 2

    # 工具被实际调用（验证 ReActSynthesizer 真的去 registry.call 了）
    graph_mock.successors.assert_called_once_with("method//M1")

    # 第 2 轮的 messages 应该包含第 1 轮 tool_call + tool_result
    # `call_args_list[1]` = 第 2 次调用的参数；`kwargs["messages"]` 是 messages 列表
    second_call_messages = llm.complete_with_tools.call_args_list[1].kwargs["messages"]
    # 至少 4 条：system + user + assistant(tool_calls) + tool(result)
    assert len(second_call_messages) >= 4
    # 最后一条应该是 tool result，role="tool"，tool_call_id="call_001"
    last_msg = second_call_messages[-1]
    assert last_msg["role"] == "tool"
    assert last_msg["tool_call_id"] == "call_001"
    # tool 内容应该是 JSON 字符串，能解出 callees
    parsed = json.loads(last_msg["content"])
    assert parsed["callees"] == ["method//B", "method//C"]

    # 最终结果解析正常
    assert len(result.sections) == 1
    assert result.sections[0]["type"] == "overview"


# ───────── RED 5: max_iterations 上限兜底 ─────────


@pytest.mark.asyncio
async def test_react_stops_at_max_iterations_when_llm_keeps_calling_tools() -> None:
    """LLM 死循环调工具时，跑到 max_iterations 就停，返回兜底答案。

    用 max_iterations=2 + 一直返回 tool_calls 来触发上限。
    """
    forever_calling = LLMToolResponse(
        content=None,
        tool_calls=[ToolCall(id="cX", name="ke_callees", arguments={"entity_id": "M"})],
    )
    llm = AsyncMock()
    # AsyncMock 的 return_value 跟 side_effect 都行；这里用 return_value 复用同一个对象
    llm.complete_with_tools = AsyncMock(return_value=forever_calling)

    # 注册 ke_callees，让 tool 能跑通
    graph_mock = MagicMock()
    graph_mock.successors.return_value = ["X"]
    from src.service.qa_engine.tools.ke_callees import build_ke_callees_tool
    registry = ToolRegistry()
    registry.register(build_ke_callees_tool(graph_mock))

    synth = ReActSynthesizer(
        llm_provider=llm,
        tool_registry=registry,
        max_iterations=2,
    )
    result = await synth.synthesize(_make_ctx())

    # LLM 被调用恰好 max_iterations 次（=2），不会跑第 3 次
    assert llm.complete_with_tools.await_count == 2

    # 返回兜底 SynthesizedAnswer：至少有一段 overview
    assert len(result.sections) >= 1
    # content 应该提示用户"达到上限"
    overview_text = result.sections[0]["content"]
    assert "上限" in overview_text or "max" in overview_text.lower()


@pytest.mark.asyncio
async def test_react_handles_tool_not_found_gracefully() -> None:
    """LLM 调用不存在的工具时，把 error 喂回让 LLM 自我修复，不抛错。"""
    # 第 1 轮 LLM 调 ke_imaginary（不存在的工具）
    round1 = LLMToolResponse(
        content=None,
        tool_calls=[ToolCall(id="cBad", name="ke_imaginary", arguments={})],
    )
    # 第 2 轮 LLM 看到 error 后给最终答案
    round2 = LLMToolResponse(content=_ok_answer_json(), tool_calls=[])
    llm = AsyncMock()
    llm.complete_with_tools = AsyncMock(side_effect=[round1, round2])

    # 空 registry
    synth = ReActSynthesizer(
        llm_provider=llm,
        tool_registry=_empty_registry(),
        max_iterations=3,
    )
    result = await synth.synthesize(_make_ctx())

    # 应该跑完 2 轮拿到 final（没崩）
    assert llm.complete_with_tools.await_count == 2

    # 第 2 轮 messages 里应该有 tool result 的 error 字段
    second_call_messages = llm.complete_with_tools.call_args_list[1].kwargs["messages"]
    last_msg = second_call_messages[-1]
    assert last_msg["role"] == "tool"
    err_payload = json.loads(last_msg["content"])
    assert "error" in err_payload
    assert "ke_imaginary" in err_payload["error"]

    # 最终结果正常
    assert len(result.sections) == 1


# ───────── RED 6: on_tool_call 回调（让 SSE 能流式发出"LLM 正在调工具"事件）─────────


@pytest.mark.asyncio
async def test_react_invokes_on_tool_call_callback_per_tool() -> None:
    """每次 tool 调用前后都触发 on_tool_call 回调，便于 SSE 实时通知前端。

    回调接收 phase ('starting'/'complete') + ToolCall + 可选 result dict。
    """
    round1 = LLMToolResponse(
        content=None,
        tool_calls=[ToolCall(id="c1", name="ke_callees", arguments={"entity_id": "M"})],
    )
    round2 = LLMToolResponse(content=_ok_answer_json(), tool_calls=[])
    llm = AsyncMock()
    llm.complete_with_tools = AsyncMock(side_effect=[round1, round2])

    graph_mock = MagicMock()
    graph_mock.successors.return_value = ["X"]
    from src.service.qa_engine.tools.ke_callees import build_ke_callees_tool
    registry = ToolRegistry()
    registry.register(build_ke_callees_tool(graph_mock))

    # 收集回调事件，便于断言
    events: list[dict] = []

    async def on_tool_call(phase: str, call, result=None):
        # phase 'starting' 时 result=None；'complete' 时是 dict
        events.append({"phase": phase, "name": call.name, "id": call.id, "result": result})

    synth = ReActSynthesizer(
        llm_provider=llm,
        tool_registry=registry,
        max_iterations=3,
    )
    await synth.synthesize(_make_ctx(), on_tool_call=on_tool_call)

    # 一次 tool 调用 → 2 个事件（starting + complete）
    assert len(events) == 2
    assert events[0]["phase"] == "starting"
    assert events[0]["name"] == "ke_callees"
    assert events[0]["id"] == "c1"
    assert events[0]["result"] is None  # starting 阶段还没结果

    assert events[1]["phase"] == "complete"
    assert events[1]["name"] == "ke_callees"
    assert events[1]["id"] == "c1"
    # complete 阶段的 result 是 tool handler 实际返回的 dict
    assert events[1]["result"]["callees"] == ["X"]


@pytest.mark.asyncio
async def test_react_skips_callback_when_none_provided() -> None:
    """没传 on_tool_call 时不报错（向后兼容；现有 test_react_loops 不传也能跑）。"""
    round1 = LLMToolResponse(
        content=None,
        tool_calls=[ToolCall(id="c1", name="ke_callees", arguments={"entity_id": "M"})],
    )
    round2 = LLMToolResponse(content=_ok_answer_json(), tool_calls=[])
    llm = AsyncMock()
    llm.complete_with_tools = AsyncMock(side_effect=[round1, round2])

    graph_mock = MagicMock()
    graph_mock.successors.return_value = []
    from src.service.qa_engine.tools.ke_callees import build_ke_callees_tool
    registry = ToolRegistry()
    registry.register(build_ke_callees_tool(graph_mock))

    synth = ReActSynthesizer(
        llm_provider=llm,
        tool_registry=registry,
        max_iterations=3,
    )
    # 不传 on_tool_call；不应抛错
    result = await synth.synthesize(_make_ctx())
    assert result.sections


# ───────── RED 7: system prompt 加 tool 使用指引 ─────────


@pytest.mark.asyncio
async def test_react_system_prompt_lists_available_tools() -> None:
    """ReActSynthesizer 的 system prompt 必须告诉 LLM 有哪些 tool 可用 +
    要利用已检索好的 candidates，避免乱编 entity_id。
    """
    llm = AsyncMock()
    llm.complete_with_tools = AsyncMock(return_value=LLMToolResponse(
        content=_ok_answer_json(), tool_calls=[]
    ))

    graph_mock = MagicMock()
    from src.service.qa_engine.tools.ke_callees import build_ke_callees_tool
    from src.service.qa_engine.tools.ke_search import build_ke_search_tool
    registry = ToolRegistry()
    registry.register(build_ke_search_tool(MagicMock()))
    registry.register(build_ke_callees_tool(graph_mock))

    synth = ReActSynthesizer(
        llm_provider=llm,
        tool_registry=registry,
        max_iterations=3,
    )
    await synth.synthesize(_make_ctx())

    # 第一轮 LLM 调用时的 system 消息内容
    first_call_messages = llm.complete_with_tools.call_args.kwargs["messages"]
    system_msg = first_call_messages[0]
    assert system_msg["role"] == "system"
    sys_text = system_msg["content"]

    # 必须提到工具名（让 LLM 知道有什么可用）
    assert "ke_search" in sys_text
    assert "ke_callees" in sys_text
    # 必须强调"用 retriever 已给的 entity_id，不要瞎编"
    assert "entity_id" in sys_text or "实体 ID" in sys_text


@pytest.mark.asyncio
async def test_react_system_prompt_omits_tool_section_when_no_tools() -> None:
    """空 registry 时不应硬塞"可用工具"段落（避免 LLM 乱发 tool_call）。"""
    llm = AsyncMock()
    llm.complete_with_tools = AsyncMock(return_value=LLMToolResponse(
        content=_ok_answer_json(), tool_calls=[]
    ))

    synth = ReActSynthesizer(
        llm_provider=llm,
        tool_registry=_empty_registry(),  # 空
        max_iterations=3,
    )
    await synth.synthesize(_make_ctx())

    sys_text = llm.complete_with_tools.call_args.kwargs["messages"][0]["content"]
    # 不应包含工具名（因为根本没注册）
    assert "ke_search" not in sys_text
    assert "ke_callees" not in sys_text


# ───────── v1.7: ReAct streaming（伪流式最终答案）─────────


@pytest.mark.asyncio
async def test_react_synthesize_stream_chunks_final_answer_through_on_token() -> None:
    """ReAct 第一轮就返回最终答案（无 tool_calls）时，
    synthesize_stream 把 content 分块通过 on_token 推出。

    v1.8 起：spec=['complete_with_tools'] 让 synthesize_stream 走 v1.7 伪流路径
    （没有 complete_stream_with_tools 时的兜底）；专门测伪流的行为。
    """
    round1 = LLMToolResponse(content=_ok_answer_json(), tool_calls=[])
    llm = MagicMock(spec=['complete_with_tools'])
    llm.complete_with_tools = AsyncMock(return_value=round1)

    synth = ReActSynthesizer(
        llm_provider=llm,
        tool_registry=_empty_registry(),
        max_iterations=3,
    )

    tokens: list[str] = []

    async def on_token(t: str) -> None:
        tokens.append(t)

    result = await synth.synthesize_stream(_make_ctx(), on_token=on_token)

    # 应该收到多个 chunk（content 比一个 chunk 长）
    assert len(tokens) >= 2
    # 拼起来等于完整 content
    assert "".join(tokens) == _ok_answer_json()
    # 结果仍是结构化答案
    assert isinstance(result, SynthesizedAnswer)
    assert len(result.sections) >= 1


@pytest.mark.asyncio
async def test_react_synthesize_stream_does_not_emit_tokens_during_tool_rounds() -> None:
    """工具调用轮（tool_calls 非空）不应触发 on_token —— 防止把 JSON arguments 当文本展示。

    v1.8 注：spec=['complete_with_tools'] 强制走 v1.7 伪流路径，专测该路径行为。
    """
    # 第 1 轮：要调工具；第 2 轮：返回最终答案
    round1 = LLMToolResponse(
        content="我先查一下…",  # 中间内容
        tool_calls=[ToolCall(id="c1", name="ke_callees", arguments={"entity_id": "M1"})],
    )
    round2 = LLMToolResponse(content=_ok_answer_json(), tool_calls=[])
    llm = MagicMock(spec=['complete_with_tools'])
    llm.complete_with_tools = AsyncMock(side_effect=[round1, round2])

    # 注册 ke_callees 让 tool 能跑
    graph_mock = MagicMock()
    graph_mock.successors.return_value = ["X"]
    from src.service.qa_engine.tools.ke_callees import build_ke_callees_tool
    registry = ToolRegistry()
    registry.register(build_ke_callees_tool(graph_mock))

    synth = ReActSynthesizer(
        llm_provider=llm,
        tool_registry=registry,
        max_iterations=3,
    )

    tokens: list[str] = []

    async def on_token(t: str) -> None:
        tokens.append(t)

    result = await synth.synthesize_stream(_make_ctx(), on_token=on_token)

    # 拼起来：只有第 2 轮的最终答案，不应包含 "我先查一下"
    full = "".join(tokens)
    assert "我先查一下" not in full
    # 应该等于第 2 轮的 content
    assert full == _ok_answer_json()
    # 结果是结构化答案
    assert len(result.sections) >= 1


# ───────── v1.8: 真 LLM 流式 (provider 支持时优先用) ─────────


@pytest.mark.asyncio
async def test_react_synthesize_stream_uses_real_streaming_when_provider_supports_it() -> None:
    """provider 有 `complete_stream_with_tools` 时，
    synthesize_stream 应该走真流路径（不再走 v1.7 伪流）：
      - 一边流入 StreamTextDelta 一边触发 on_token
      - 流末解析的 ToolCall 列表当作 tool_calls 执行
    """
    from src.service.qa_engine.llm_types import StreamTextDelta

    # 用 async generator function 直接做 mock（AsyncMock 处理 async generator 不直观）
    # 第一轮：流式输出"我先" + "查"两个文本片段 + 1 个 tool_call（ke_callees）
    async def fake_stream_round1(**kwargs):
        yield StreamTextDelta(text="我先")
        yield StreamTextDelta(text="查")
        yield ToolCall(id="c1", name="ke_callees", arguments={"entity_id": "M1"})

    # 第二轮：纯流式输出最终答案（无 tool_calls）
    async def fake_stream_round2(**kwargs):
        # 把 _ok_answer_json 拆成几个 chunk 模拟流式
        full = _ok_answer_json()
        # 拆 3 段
        third = len(full) // 3
        yield StreamTextDelta(text=full[:third])
        yield StreamTextDelta(text=full[third:2*third])
        yield StreamTextDelta(text=full[2*third:])

    # llm 是 MagicMock，但 complete_stream_with_tools 是 async generator function
    llm = MagicMock()
    # `side_effect` 让每次调用返回下一个 generator
    llm.complete_stream_with_tools = MagicMock(side_effect=[fake_stream_round1(), fake_stream_round2()])

    # 注册 ke_callees tool
    graph_mock = MagicMock()
    graph_mock.successors.return_value = ["B"]
    from src.service.qa_engine.tools.ke_callees import build_ke_callees_tool
    registry = ToolRegistry()
    registry.register(build_ke_callees_tool(graph_mock))

    synth = ReActSynthesizer(
        llm_provider=llm,
        tool_registry=registry,
        max_iterations=3,
    )

    tokens: list[str] = []

    async def on_token(t: str) -> None:
        tokens.append(t)

    result = await synth.synthesize_stream(_make_ctx(), on_token=on_token)

    # 真流式应该走过两轮 complete_stream_with_tools（非 complete_with_tools）
    assert llm.complete_stream_with_tools.call_count == 2

    # tokens 应包含两轮的所有 text deltas：第一轮"我先" + "查"，第二轮的 JSON 三段
    # 拼起来：第一轮文本 + 第二轮完整 JSON
    full_stream = "".join(tokens)
    assert "我先" in full_stream
    assert "查" in full_stream
    assert _ok_answer_json() in full_stream

    # 工具被实际调用（ke_callees on M1）
    graph_mock.successors.assert_called_once_with("M1")

    # 最终结果解析正常
    assert isinstance(result, SynthesizedAnswer)
    assert len(result.sections) >= 1


@pytest.mark.asyncio
async def test_react_synthesize_stream_falls_back_to_pseudo_when_provider_lacks_stream() -> None:
    """provider 没实现 complete_stream_with_tools 时，回退 v1.7 伪流策略
    （complete_with_tools + _pseudo_stream final answer）。
    """
    # llm 只有 complete_with_tools，**没有** complete_stream_with_tools
    # 用 spec 限制 mock 可访问属性
    llm = MagicMock(spec=['complete_with_tools'])
    llm.complete_with_tools = AsyncMock(return_value=LLMToolResponse(
        content=_ok_answer_json(), tool_calls=[],
    ))

    synth = ReActSynthesizer(
        llm_provider=llm,
        tool_registry=_empty_registry(),
        max_iterations=3,
    )

    tokens: list[str] = []

    async def on_token(t: str) -> None:
        tokens.append(t)

    result = await synth.synthesize_stream(_make_ctx(), on_token=on_token)

    # 走的是 v1.7 老路径：complete_with_tools 被调，伪流式分块
    assert llm.complete_with_tools.await_count == 1
    # 还是有 tokens（伪流式分了块）
    assert "".join(tokens) == _ok_answer_json()
    assert isinstance(result, SynthesizedAnswer)


@pytest.mark.asyncio
async def test_react_synthesize_stream_forwards_tool_call_callback() -> None:
    """on_tool_call 仍然在 tool 执行前后触发（v1.3 行为保留）。"""
    round1 = LLMToolResponse(
        content=None,
        tool_calls=[ToolCall(id="c1", name="ke_callees", arguments={"entity_id": "M"})],
    )
    round2 = LLMToolResponse(content=_ok_answer_json(), tool_calls=[])
    llm = MagicMock(spec=['complete_with_tools'])
    llm.complete_with_tools = AsyncMock(side_effect=[round1, round2])

    graph_mock = MagicMock()
    graph_mock.successors.return_value = []
    from src.service.qa_engine.tools.ke_callees import build_ke_callees_tool
    registry = ToolRegistry()
    registry.register(build_ke_callees_tool(graph_mock))

    synth = ReActSynthesizer(
        llm_provider=llm,
        tool_registry=registry,
        max_iterations=3,
    )

    tool_events: list[dict] = []

    async def on_tool_call(phase: str, call, result=None) -> None:
        tool_events.append({"phase": phase, "name": call.name})

    await synth.synthesize_stream(
        _make_ctx(),
        on_token=None,
        on_tool_call=on_tool_call,
    )

    # 至少 2 个事件（starting + complete）
    assert len(tool_events) == 2
    assert tool_events[0]["phase"] == "starting"
    assert tool_events[1]["phase"] == "complete"


def test_react_synthesizer_default_max_iterations_is_12():
    """安全阀：默认循环上限 12（放开旧的 3，支撑多跳分析跑到收敛）。"""
    from src.service.qa_engine.react_synthesizer import ReActSynthesizer

    # 构造只需 llm + tool_registry（鸭子类型，传占位即可；本测试不跑循环）
    class _DummyLLM:
        async def complete_with_tools(self, *, messages, tools):
            raise NotImplementedError

    from src.service.qa_engine.tools.base import ToolRegistry

    synth = ReActSynthesizer(llm_provider=_DummyLLM(), tool_registry=ToolRegistry())
    assert synth.max_iterations == 12


@pytest.mark.asyncio
async def test_synthesize_stream_forwards_thinking_to_on_thinking():
    """真流式循环把 StreamThinkingDelta 转发到 on_thinking 回调（StreamTextDelta 仍走 on_token）。"""
    from src.service.qa_engine.react_synthesizer import ReActSynthesizer
    from src.service.qa_engine.tools.base import ToolRegistry
    from src.service.qa_engine.llm_types import StreamTextDelta, StreamThinkingDelta
    from src.service.qa_engine.retriever import RetrievedContext

    # fake LLM：有 complete_stream_with_tools（走真流路径），先吐思考再吐答案、无 tool_calls（即 final）
    class _FakeStreamLLM:
        async def complete_stream_with_tools(self, *, messages, tools):
            yield StreamThinkingDelta(text="先看调用方")
            yield StreamTextDelta(text="## 概述\n答案正文")

    synth = ReActSynthesizer(
        llm_provider=_FakeStreamLLM(),
        tool_registry=ToolRegistry(),
        max_iterations=3,
    )

    thinking_chunks: list[str] = []
    token_chunks: list[str] = []

    async def _on_thinking(t): thinking_chunks.append(t)
    async def _on_token(t): token_chunks.append(t)

    # RetrievedContext 是 dataclass：question + project_id 必填，其余字段有默认值
    ctx = RetrievedContext(question="VetController 调了谁？", project_id="proj-a")
    await synth.synthesize_stream(
        ctx, history=[], on_token=_on_token, on_thinking=_on_thinking,
    )

    assert "".join(thinking_chunks) == "先看调用方"
    assert "".join(token_chunks) == "## 概述\n答案正文"


@pytest.mark.asyncio
async def test_synthesize_stream_collects_cited_entities():
    """agent 查过的 entity_id 被收集进 SynthesizedAnswer.cited_entities（去重、跨轮累积）。"""
    from src.service.qa_engine.react_synthesizer import ReActSynthesizer
    from src.service.qa_engine.tools.base import Tool, ToolRegistry
    from src.service.qa_engine.llm_types import StreamTextDelta, ToolCall
    from src.service.qa_engine.retriever import RetrievedContext

    # 注册一个假工具，handler 直接回 echo（不需真后端）
    async def _echo_handler(inp):
        return {"entity_id": inp.get("entity_id"), "ok": True}

    reg = ToolRegistry()
    reg.register(Tool(
        name="ke_callees",
        description="x",
        input_schema={"type": "object", "properties": {"entity_id": {"type": "string"}}, "required": ["entity_id"]},
        handler=_echo_handler,
    ))

    # fake LLM：第 1 轮调 ke_callees(method//A)，第 2 轮再调 ke_callees(method//B)，第 3 轮给 final
    class _FakeLLM:
        def __init__(self):
            self._round = 0

        async def complete_stream_with_tools(self, *, messages, tools):
            self._round += 1
            if self._round == 1:
                yield ToolCall(id="c1", name="ke_callees", arguments={"entity_id": "method//A"})
            elif self._round == 2:
                yield ToolCall(id="c2", name="ke_callees", arguments={"entity_id": "method//B"})
            else:
                yield StreamTextDelta(text="## 概述\n答案")

    synth = ReActSynthesizer(llm_provider=_FakeLLM(), tool_registry=reg, max_iterations=5)
    ctx = RetrievedContext(question="q", project_id="proj-a")
    answer = await synth.synthesize_stream(ctx, history=[])

    assert answer.cited_entities == ["method//A", "method//B"]
