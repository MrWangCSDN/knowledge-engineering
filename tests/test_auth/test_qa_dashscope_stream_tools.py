"""验证 DashScope `complete_stream_with_tools` 的 chunk 解析。

OpenAI 兼容协议下，流式 + tool calling 的响应格式：

  data: {"choices":[{"delta":{"role":"assistant"},"finish_reason":null,"index":0}]}     ← role 行
  data: {"choices":[{"delta":{"content":"思考"},"finish_reason":null,"index":0}]}       ← text 增量
  data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_x","type":"function",
                                "function":{"name":"ke_callees","arguments":""}}]},
                     "finish_reason":null,"index":0}]}                                  ← tool_call 开始
  data: {"choices":[{"delta":{"tool_calls":[{"index":0,
                                "function":{"arguments":"{\\\"entity_id\\\":"}}]},
                     "finish_reason":null,"index":0}]}                                  ← tool_call arguments 续传
  data: {"choices":[{"delta":{"tool_calls":[{"index":0,
                                "function":{"arguments":"\\\"M1\\\"}"}}]},
                     "finish_reason":null,"index":0}]}                                  ← arguments 续完
  data: {"choices":[{"delta":{},"finish_reason":"tool_calls","index":0}]}               ← 结束信号
  data: [DONE]                                                                          ← 关闭信号

`_process_stream_line(line, pending)` 是一个**有状态**的解析器（用 pending 字典累积 tool_calls）：
  - 输入 SSE 单行 + 当前 pending 状态
  - 输出本行触发的 events 列表（可能空、可能 1 个 text、可能 1 个或多个 tool_call）
  - **mutates** pending（在 tool_call 进行中时累计 arguments）
"""
import pytest

# 待添加的类型 + 解析函数
from src.service.qa_engine.llm_types import StreamTextDelta, ToolCall
from src.service.qa_engine.llm_dashscope import DashScopeProvider


def test_process_stream_line_yields_text_delta_for_content() -> None:
    """content delta 行 → yield 一个 StreamTextDelta。"""
    pending: dict = {}
    line = 'data: {"choices":[{"delta":{"content":"你好"},"finish_reason":null,"index":0}]}'
    events = DashScopeProvider._process_stream_line(line, pending)
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, StreamTextDelta)
    assert ev.text == "你好"
    # pending 没被修改（这一行是 text 不是 tool）
    assert pending == {}


def test_process_stream_line_buffers_tool_call_starting_no_event() -> None:
    """tool_call 第一行（带 id + name + 空 arguments）应该被缓存到 pending，
    暂时不 yield（要等 finish_reason）。
    """
    pending: dict = {}
    line = ('data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_X","type":"function",'
            '"function":{"name":"ke_callees","arguments":""}}]},"finish_reason":null,"index":0}]}')
    events = DashScopeProvider._process_stream_line(line, pending)
    # 这一行不 yield
    assert events == []
    # 但 pending[0] 已经记录了 id / name
    assert 0 in pending
    assert pending[0]["id"] == "call_X"
    assert pending[0]["name"] == "ke_callees"
    assert pending[0]["args_buffer"] == ""


def test_process_stream_line_appends_arguments_fragment_no_event() -> None:
    """tool_call 续传行：只附加 arguments fragment 到 pending，仍不 yield。"""
    # 先模拟上一行已经写入 pending
    pending = {0: {"id": "call_X", "name": "ke_callees", "args_buffer": '{"entity_id":'}}
    line = ('data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
            '"function":{"arguments":"\\"M1\\"}"}}]},"finish_reason":null,"index":0}]}')
    events = DashScopeProvider._process_stream_line(line, pending)
    assert events == []
    # arguments 拼接
    assert pending[0]["args_buffer"] == '{"entity_id":"M1"}'


def test_process_stream_line_finish_reason_tool_calls_yields_assembled_tool() -> None:
    """finish_reason="tool_calls" → yield 装配好的 ToolCall(s)，清空 pending。"""
    pending = {0: {"id": "call_X", "name": "ke_callees", "args_buffer": '{"entity_id":"M1"}'}}
    line = 'data: {"choices":[{"delta":{},"finish_reason":"tool_calls","index":0}]}'
    events = DashScopeProvider._process_stream_line(line, pending)
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, ToolCall)
    assert ev.id == "call_X"
    assert ev.name == "ke_callees"
    # arguments 已经 json.loads 成 dict
    assert ev.arguments == {"entity_id": "M1"}
    # pending 清空
    assert pending == {}


def test_process_stream_line_finish_reason_stop_yields_nothing() -> None:
    """finish_reason='stop'（普通完成）→ 无 tool_calls 要 flush，啥也不 yield。"""
    pending: dict = {}
    line = 'data: {"choices":[{"delta":{},"finish_reason":"stop","index":0}]}'
    events = DashScopeProvider._process_stream_line(line, pending)
    assert events == []


def test_process_stream_line_done_signal_yields_nothing() -> None:
    """`[DONE]` 终止信号 → 不 yield；调用方循环看到这个就停。"""
    pending: dict = {}
    events = DashScopeProvider._process_stream_line("data: [DONE]", pending)
    assert events == []


def test_process_stream_line_keepalive_and_invalid_lines() -> None:
    """空行 / 注释行 / 不合法 JSON → 跳过，不抛错。"""
    pending: dict = {}
    assert DashScopeProvider._process_stream_line("", pending) == []
    assert DashScopeProvider._process_stream_line(":keep-alive", pending) == []
    assert DashScopeProvider._process_stream_line("data: {bad json", pending) == []


def test_process_stream_line_malformed_arguments_falls_back_to_empty_dict() -> None:
    """tool_call args_buffer 不是合法 JSON 时，ToolCall.arguments 兜底 {}（跟 v1.3 非流式版本一致）。"""
    pending = {0: {"id": "c1", "name": "x", "args_buffer": "this is not json"}}
    line = 'data: {"choices":[{"delta":{},"finish_reason":"tool_calls","index":0}]}'
    events = DashScopeProvider._process_stream_line(line, pending)
    assert len(events) == 1
    assert events[0].arguments == {}


# ───────── complete_stream_with_tools 整体流程 ─────────


@pytest.mark.asyncio
async def test_complete_stream_with_tools_text_then_tool_call_sequence() -> None:
    """完整流程：mock httpx 响应一系列 SSE 行，验证 yield 顺序：
       2 段 text → 装配好的 ToolCall → 完结。
    """
    # 构造 SSE 响应的行序列；模拟"先吐 2 个文本片段，再调一个 ke_callees 工具"的场景
    sse_lines = [
        # 第一个文本片段
        'data: {"choices":[{"delta":{"content":"我先"},"finish_reason":null,"index":0}]}',
        'data: {"choices":[{"delta":{"content":"查查"},"finish_reason":null,"index":0}]}',
        # tool_call 头一帧（带 id / name / 空 args）
        ('data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_X","type":"function",'
         '"function":{"name":"ke_callees","arguments":""}}]},"finish_reason":null,"index":0}]}'),
        # arguments 续传两次
        ('data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
         '"function":{"arguments":"{\\"entity_id\\":"}}]},"finish_reason":null,"index":0}]}'),
        ('data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
         '"function":{"arguments":"\\"M1\\"}"}}]},"finish_reason":null,"index":0}]}'),
        # 完结信号
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls","index":0}]}',
        'data: [DONE]',
    ]

    # mock httpx：返回支持 aiter_lines 的假 response
    class _FakeResponse:
        def __init__(self, lines):
            self._lines = lines

        def raise_for_status(self):
            pass

        async def aiter_lines(self):
            for line in self._lines:
                yield line

    class _FakeStreamCtx:
        def __init__(self, lines):
            self._lines = lines

        async def __aenter__(self):
            return _FakeResponse(self._lines)

        async def __aexit__(self, *args):
            return False

    class _FakeClient:
        def __init__(self, lines):
            self._lines = lines

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, *args, **kwargs):
            return _FakeStreamCtx(self._lines)

    # 把 httpx.AsyncClient 替换成假的（monkeypatch 风格）
    from unittest.mock import patch

    provider = DashScopeProvider(api_key="fake-key", model="qwen-plus")

    async def collect_events():
        events = []
        # patch context：让 httpx.AsyncClient 返回我们的假 client
        with patch("httpx.AsyncClient", lambda *a, **kw: _FakeClient(sse_lines)):
            async for ev in provider.complete_stream_with_tools(
                messages=[{"role": "user", "content": "test"}],
                tools=[{"type": "function", "function": {"name": "ke_callees"}}],
            ):
                events.append(ev)
        return events

    events = await collect_events()

    # 应该 yield 出：StreamTextDelta("我先") / StreamTextDelta("查查") / ToolCall(...)
    assert len(events) == 3
    assert isinstance(events[0], StreamTextDelta)
    assert events[0].text == "我先"
    assert isinstance(events[1], StreamTextDelta)
    assert events[1].text == "查查"
    assert isinstance(events[2], ToolCall)
    assert events[2].id == "call_X"
    assert events[2].name == "ke_callees"
    assert events[2].arguments == {"entity_id": "M1"}


@pytest.mark.asyncio
async def test_complete_stream_with_tools_pure_text_no_tool_calls() -> None:
    """纯文本响应（无 tool_calls）：yield 多个 StreamTextDelta 后正常结束。"""
    sse_lines = [
        'data: {"choices":[{"delta":{"content":"答"},"finish_reason":null,"index":0}]}',
        'data: {"choices":[{"delta":{"content":"案"},"finish_reason":null,"index":0}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop","index":0}]}',
        'data: [DONE]',
    ]

    class _FakeResponse:
        def __init__(self, lines): self._lines = lines
        def raise_for_status(self): pass
        async def aiter_lines(self):
            for line in self._lines:
                yield line

    class _FakeStreamCtx:
        def __init__(self, lines): self._lines = lines
        async def __aenter__(self): return _FakeResponse(self._lines)
        async def __aexit__(self, *args): return False

    class _FakeClient:
        def __init__(self, lines): self._lines = lines
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        def stream(self, *args, **kwargs): return _FakeStreamCtx(self._lines)

    from unittest.mock import patch

    provider = DashScopeProvider(api_key="fake-key")

    events = []
    with patch("httpx.AsyncClient", lambda *a, **kw: _FakeClient(sse_lines)):
        async for ev in provider.complete_stream_with_tools(
            messages=[{"role": "user", "content": "x"}],
            tools=[],
        ):
            events.append(ev)

    # 2 个 text，没有 tool_call
    assert len(events) == 2
    assert all(isinstance(ev, StreamTextDelta) for ev in events)
    assert events[0].text == "答"
    assert events[1].text == "案"
