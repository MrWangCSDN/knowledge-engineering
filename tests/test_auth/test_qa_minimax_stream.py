# tests/test_auth/test_qa_minimax_stream.py
"""
MiniMaxProvider.complete_stream：剥掉 <think>...</think> 段只吐正文（历史行为）。
此前无测试覆盖；本文件先锁行为，再支撑 ThinkSplitter 重构不回归。
"""
import pytest

from src.service.qa_engine.llm_minimax import MiniMaxProvider


async def _fake_parent_stream(chunks):
    """模拟父类 DashScopeProvider.complete_stream 逐 chunk yield 字符串。"""
    for c in chunks:
        yield c


@pytest.mark.asyncio
async def test_complete_stream_strips_think(monkeypatch):
    # 构造 provider（api_key 给假值绕过 __init__ 校验）
    provider = MiniMaxProvider(api_key="test-key")

    # monkeypatch 父类 complete_stream，让它吐带 <think> 的分片
    chunks = ["答案前<think>这是推", "理过程</think>答案后"]

    # 替换父类绑定方法（MiniMax.complete_stream 内部调 super().complete_stream）
    # super() 沿 MRO 查到被 patch 的 DashScopeProvider.complete_stream
    monkeypatch.setattr(
        "src.service.qa_engine.llm_dashscope.DashScopeProvider.complete_stream",
        lambda self, *, system, user, **kwargs: _fake_parent_stream(chunks),
    )

    out = []
    async for tok in provider.complete_stream(system="s", user="u"):
        out.append(tok)

    # think 段被剥掉，只剩正文
    assert "".join(out) == "答案前答案后"
    assert "<think>" not in "".join(out)
    assert "推理过程" not in "".join(out)


@pytest.mark.asyncio
async def test_stream_with_tools_routes_think_to_thinking_delta(monkeypatch):
    from src.service.qa_engine.llm_types import (
        StreamTextDelta,
        StreamThinkingDelta,
        ToolCall,
    )

    provider = MiniMaxProvider(api_key="test-key")

    # 模拟父类 complete_stream_with_tools 吐出的事件序列：
    # 正文 StreamTextDelta（含跨片 <think>）+ 一个 ToolCall
    async def fake_parent_events(*, messages, tools, **kwargs):
        yield StreamTextDelta(text="答案前<think>推")
        yield StreamTextDelta(text="理段</think>答案后")
        yield ToolCall(id="c1", name="ke_search", arguments={"query": "x"})

    monkeypatch.setattr(
        "src.service.qa_engine.llm_dashscope.DashScopeProvider.complete_stream_with_tools",
        lambda self, *, messages, tools, **kwargs: fake_parent_events(
            messages=messages, tools=tools, **kwargs
        ),
    )

    events = []
    async for ev in provider.complete_stream_with_tools(messages=[], tools=[]):
        events.append(ev)

    # think 段被路由到 StreamThinkingDelta
    think = "".join(e.text for e in events if isinstance(e, StreamThinkingDelta))
    text = "".join(e.text for e in events if isinstance(e, StreamTextDelta))
    tool_calls = [e for e in events if isinstance(e, ToolCall)]

    assert think == "推理段"
    assert text == "答案前答案后"
    # ToolCall 原样透传
    assert len(tool_calls) == 1
    assert tool_calls[0].name == "ke_search"
    assert tool_calls[0].arguments == {"query": "x"}
