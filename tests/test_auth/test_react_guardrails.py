"""护栏：默认 max_iterations=8；单工具超时转 error 信号不抛。"""
import asyncio
from src.service.qa_engine.react_synthesizer import ReActSynthesizer
from src.service.qa_engine.tools.base import Tool, ToolRegistry


def test_default_max_iterations_is_8():
    reg = ToolRegistry()
    synth = ReActSynthesizer(llm_provider=object(), tool_registry=reg)
    assert synth.max_iterations == 8


def test_single_tool_timeout_returns_error_signal():
    # 注册一个永远 hang 的工具，单工具超时应返回 {"error":...} 而非卡死/抛异常
    async def _hang(_input):
        await asyncio.sleep(10)
        return {}
    reg = ToolRegistry()
    reg.register(Tool(name="ke_hang", description="hang", input_schema={"type": "object"}, handler=_hang))
    synth = ReActSynthesizer(llm_provider=object(), tool_registry=reg, tool_timeout_sec=0.05)

    class _TC:
        id = "1"; name = "ke_hang"; arguments = {}
    out = asyncio.run(synth._execute_tool_call(_TC()))
    assert "error" in out and "timeout" in out["error"].lower()
