# tests/test_auth/test_qa_stream_thinking.py
"""
StreamThinkingDelta：流式 thinking 增量类型（设计 §5）。
与 StreamTextDelta 区分——前端据此把 thinking 渲染成灰字。
"""
from src.service.qa_engine.llm_types import StreamThinkingDelta, StreamTextDelta


def test_thinking_delta_holds_text():
    # 思考增量携带一段推理文本
    d = StreamThinkingDelta(text="我需要先查 OrderService 的调用方")
    assert d.text == "我需要先查 OrderService 的调用方"


def test_thinking_delta_is_distinct_type_from_text_delta():
    # 类型必须可区分——上层 isinstance 分流到不同 SSE 通道
    t = StreamThinkingDelta(text="思考")
    a = StreamTextDelta(text="答案")
    assert not isinstance(t, StreamTextDelta)
    assert not isinstance(a, StreamThinkingDelta)


def test_thinking_delta_is_frozen():
    # frozen dataclass：构造后不可改（与 StreamTextDelta 同约束）
    import dataclasses
    d = StreamThinkingDelta(text="x")
    try:
        d.text = "y"  # type: ignore[misc]
        assert False, "应抛 FrozenInstanceError"
    except dataclasses.FrozenInstanceError:
        pass
