"""_on_tool_call complete 阶段：结果含 render → tool_call payload 带 render（供前端内联渲染）。

说明：_on_tool_call 是 stream_qa_answer 内部闭包，不可直接 import；
沿用本仓库源码不变量手法（见 chat.ts SSE parser 类比），断言 complete 分支透传 render。
"""
from pathlib import Path

_SRC = Path("src/service/qa_engine/sse_emitter.py").read_text(encoding="utf-8")


def test_on_tool_call_passes_render():
    i = _SRC.index("async def _on_tool_call")
    j = _SRC.index('pending_tool_events.append(("tool_call"', i)
    body = _SRC[i:j + 200]
    # complete 分支把 result 里的 render 透传进 payload
    assert 'payload["render"]' in body          # 透传 render 进 payload
    assert 'result.get("render")' in body       # 判定 render 非空才透传（调查类无 render 不受影响）
