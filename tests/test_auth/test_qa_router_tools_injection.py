# tests/test_auth/test_qa_router_tools_injection.py
"""
qa_router._inject_per_request_tool_registry：ReAct synthesizer 按 project_id
注入 per-request 工具 registry（修 Task 24 多租户隔离）；非 ReAct 跳过；失败不抛。
"""
from src.service.qa_engine.tools.base import ToolRegistry
from src.service.qa_engine.react_synthesizer import ReActSynthesizer
from src.service import qa_router


class _DummyLLM:
    async def complete_with_tools(self, *, messages, tools):
        raise NotImplementedError


def test_injects_registry_into_react_synthesizer(monkeypatch):
    sentinel = ToolRegistry()
    # monkeypatch build_tools_for_project 返回 sentinel（不碰真实 Weaviate/Neo4j）
    monkeypatch.setattr(
        qa_router, "build_tools_for_project",
        lambda project_id, request: sentinel,
    )
    synth = ReActSynthesizer(llm_provider=_DummyLLM(), tool_registry=ToolRegistry())
    out = qa_router._inject_per_request_tool_registry(synth, "proj-a", object())
    # registry 被换成 per-request 的 sentinel
    assert out.tool_registry is sentinel


def test_skips_non_react_synthesizer(monkeypatch):
    # 非 ReActSynthesizer（用普通对象模拟 QASynthesizer）→ 原样返回，不调 builder
    called = {"n": 0}

    def _builder(project_id, request):
        called["n"] += 1
        return ToolRegistry()

    monkeypatch.setattr(qa_router, "build_tools_for_project", _builder)

    class _PlainSynth:
        pass

    plain = _PlainSynth()
    out = qa_router._inject_per_request_tool_registry(plain, "proj-a", object())
    assert out is plain
    assert called["n"] == 0  # 非 ReAct 不构造 registry


def test_builder_failure_does_not_raise(monkeypatch):
    # builder 抛错（后端未就绪）→ 不抛，沿用 synthesizer 原 registry
    original = ToolRegistry()

    def _boom(project_id, request):
        raise RuntimeError("backend down")

    monkeypatch.setattr(qa_router, "build_tools_for_project", _boom)
    synth = ReActSynthesizer(llm_provider=_DummyLLM(), tool_registry=original)
    out = qa_router._inject_per_request_tool_registry(synth, "proj-a", object())
    # 失败兜底：registry 仍是原来的，没被清成 None
    assert out.tool_registry is original
