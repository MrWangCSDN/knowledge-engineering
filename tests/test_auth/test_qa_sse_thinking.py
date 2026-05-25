# tests/test_auth/test_qa_sse_thinking.py
"""
sse_emitter 的 thinking SSE 事件接线（设计 §8）。
stream_qa_answer 是重依赖异步生成器，沿用本仓源码不变量手法校验装配契约：
  - 构造了 _on_thinking 回调
  - is_react 时把 on_thinking 传进 stream_kwargs
  - pending_thinking 作为 'thinking' 事件 flush
synthesize_stream 的 on_thinking 真行为由 test_qa_react_synthesizer 覆盖。
"""
from pathlib import Path


def _src() -> str:
    return Path("src/service/qa_engine/sse_emitter.py").read_text(encoding="utf-8")


def test_sse_emitter_defines_on_thinking_callback():
    src = _src()
    assert "pending_thinking" in src
    assert "async def _on_thinking" in src


def test_sse_emitter_passes_on_thinking_to_stream_kwargs():
    src = _src()
    assert 'stream_kwargs["on_thinking"]' in src


def test_sse_emitter_flushes_thinking_event():
    src = _src()
    assert 'format_sse("thinking"' in src
