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


def test_build_tools_for_project_passes_optional_stores(monkeypatch):
    """build_tools_for_project 把 app.state 的 code_store / method_interp_store
    透传给 build_default_registry（有则注册对应工具）。"""
    captured = {}

    code_sentinel = object()
    interp_sentinel = object()

    def _fake_build_default_registry(*, graph, business_store, project_id, code_store=None, method_interp_store=None):
        captured["code_store"] = code_store
        captured["method_interp_store"] = method_interp_store
        from src.service.qa_engine.tools.base import ToolRegistry
        return ToolRegistry()

    # 伪造 request.app.state：含 4 个后端
    class _State:
        weaviate_business_store = object()
        neo4j_backend = object()
        weaviate_code_store = code_sentinel
        weaviate_method_interp_store = interp_sentinel

    class _App:
        state = _State()

    class _Req:
        app = _App()

    # monkeypatch build_default_registry 捕获透传；adapter 构造换轻量替身（不真连后端）
    # 注意：build_tools_for_project 内部是局部 import
    #   `from src.service.qa_engine.tools import build_default_registry`
    #   `from src.service.qa_engine.adapters import Neo4jGraphAdapter, WeaviateBusinessAdapter`
    # 局部 import 在调用时按模块属性取值 → 必须 patch 这两个**源模块**的属性。
    import src.service.qa_engine.tools as _tools_mod
    import src.service.qa_engine.adapters as _adapters
    monkeypatch.setattr(_tools_mod, "build_default_registry", _fake_build_default_registry)
    monkeypatch.setattr(_adapters, "Neo4jGraphAdapter", lambda backend, project_id: object())
    monkeypatch.setattr(_adapters, "WeaviateBusinessAdapter", lambda store: object())

    qa_router.build_tools_for_project("proj-a", _Req())
    assert captured["code_store"] is code_sentinel
    assert captured["method_interp_store"] is interp_sentinel


def test_build_tools_for_project_passes_project_id_to_registry(monkeypatch):
    """build_tools_for_project 把 URL path 的 project_id 闭包给 ke_search。

    关键不变量：ke_search 收到 LLM 的 input 即使**没**含 project_id 也能正常用闭包查；
    即使 LLM 误传 project_id="wrong" 也被忽略。

    ke_search handler 调用 WeaviateBusinessAdapter.search_method_hits_by_text；
    这里 monkeypatch WeaviateBusinessAdapter 使其返回带 spy 的替身，验证 project_id 是闭包值。
    """
    from unittest.mock import MagicMock
    import asyncio
    import src.service.qa_engine.adapters as _adapters

    # spy_adapter：search_method_hits_by_text 有记录功能，不真连 Weaviate
    spy_adapter = MagicMock()
    spy_adapter.search_method_hits_by_text.return_value = []

    # monkeypatch WeaviateBusinessAdapter 构造，让它直接返回 spy_adapter
    monkeypatch.setattr(
        _adapters, "WeaviateBusinessAdapter",
        lambda store: spy_adapter,
    )
    # Neo4jGraphAdapter 也替换成轻量替身（不真连 Neo4j）
    monkeypatch.setattr(
        _adapters, "Neo4jGraphAdapter",
        lambda backend, project_id: MagicMock(),
    )

    # 伪造 request.app.state：含 4 个后端（值是什么不重要，adapter 已被 patch）
    class _State:
        weaviate_business_store = object()
        neo4j_backend = object()
        weaviate_code_store = None
        weaviate_method_interp_store = None

    class _App:
        state = _State()

    class _Req:
        app = _App()

    registry = qa_router.build_tools_for_project("mall-swarm", _Req())
    ke_search = registry.get("ke_search")

    # LLM 误传 wrong-tenant 也忽略，用 URL path 闭包的 mall-swarm
    # asyncio.run 替代 get_event_loop().run_until_complete() — Python 3.12 full-suite 兼容
    asyncio.run(ke_search.handler({"query": "X", "project_id": "wrong-tenant"}))

    spy_adapter.search_method_hits_by_text.assert_called_once()
    call_kwargs = spy_adapter.search_method_hits_by_text.call_args.kwargs
    assert call_kwargs["project_id"] == "mall-swarm"
