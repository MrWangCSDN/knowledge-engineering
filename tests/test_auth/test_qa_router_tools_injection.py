# tests/test_auth/test_qa_router_tools_injection.py
"""
qa_router._inject_per_request_tool_registry：ReAct synthesizer 按 project_id
注入 per-request 工具 registry（修 Task 24 多租户隔离）；非 ReAct 跳过；失败不抛。
"""
import pytest
from src.service.qa_engine.tools.base import ToolRegistry
from src.service.qa_engine.react_synthesizer import ReActSynthesizer
from src.service import qa_router


class _DummyLLM:
    async def complete_with_tools(self, *, messages, tools):
        raise NotImplementedError


@pytest.mark.asyncio
async def test_injects_registry_into_react_synthesizer(monkeypatch):
    from unittest.mock import AsyncMock
    sentinel = ToolRegistry()
    # monkeypatch build_tools_for_project 返回 sentinel（不碰真实 Weaviate/Neo4j）
    # v2.x：build_tools_for_project 是 async，monkeypatch 用 AsyncMock 协程
    async def _fake_builder(project_id, request, db):
        return sentinel
    monkeypatch.setattr(qa_router, "build_tools_for_project", _fake_builder)
    fake_db = AsyncMock()
    synth = ReActSynthesizer(llm_provider=_DummyLLM(), tool_registry=ToolRegistry())
    out = await qa_router._inject_per_request_tool_registry(synth, "proj-a", object(), fake_db)
    # registry 被换成 per-request 的 sentinel
    assert out.tool_registry is sentinel


@pytest.mark.asyncio
async def test_skips_non_react_synthesizer(monkeypatch):
    # 非 ReActSynthesizer（用普通对象模拟 QASynthesizer）→ 原样返回，不调 builder
    from unittest.mock import AsyncMock
    called = {"n": 0}

    async def _builder(project_id, request, db):
        called["n"] += 1
        return ToolRegistry()

    monkeypatch.setattr(qa_router, "build_tools_for_project", _builder)

    class _PlainSynth:
        pass

    fake_db = AsyncMock()
    plain = _PlainSynth()
    out = await qa_router._inject_per_request_tool_registry(plain, "proj-a", object(), fake_db)
    assert out is plain
    assert called["n"] == 0  # 非 ReAct 不构造 registry


@pytest.mark.asyncio
async def test_builder_failure_does_not_raise(monkeypatch):
    # builder 抛错（后端未就绪）→ 不抛，沿用 synthesizer 原 registry
    from unittest.mock import AsyncMock
    original = ToolRegistry()

    async def _boom(project_id, request, db):
        raise RuntimeError("backend down")

    monkeypatch.setattr(qa_router, "build_tools_for_project", _boom)
    fake_db = AsyncMock()
    synth = ReActSynthesizer(llm_provider=_DummyLLM(), tool_registry=original)
    out = await qa_router._inject_per_request_tool_registry(synth, "proj-a", object(), fake_db)
    # 失败兜底：registry 仍是原来的，没被清成 None
    assert out.tool_registry is original


@pytest.mark.asyncio
async def test_build_tools_for_project_passes_optional_stores(monkeypatch):
    """build_tools_for_project 把 app.state 的 code_store / method_interp_store
    透传给 build_default_registry（有则注册对应工具）。"""
    from unittest.mock import AsyncMock
    captured = {}

    code_sentinel = object()
    interp_sentinel = object()

    def _fake_build_default_registry(*, graph, interpretation_store, project_id, code_store=None, method_interp_store=None, repo_local_path=None):
        captured["code_store"] = code_store
        captured["method_interp_store"] = method_interp_store
        from src.service.qa_engine.tools.base import ToolRegistry
        return ToolRegistry()

    # 伪造 request.app.state：CG-T1.6 后结构导航走 CodeGraph，不再需要 neo4j_backend
    interp_sentinel_store = object()

    class _State:
        weaviate_code_store = code_sentinel
        weaviate_interp_store = interp_sentinel_store

    class _App:
        state = _State()

    class _Req:
        app = _App()

    # v2.x：build_tools_for_project 是 async，需要 await + 传 db 参数
    # db.get 返回 None（repo_local_path 降级 None，不影响 code_store/method_interp_store 透传测试）
    fake_db = AsyncMock()
    fake_db.get = AsyncMock(return_value=None)

    # monkeypatch build_default_registry 捕获透传；adapter 构造换轻量替身（不真连后端）
    # 注意：build_tools_for_project 内部是局部 import，局部 import 在调用时按模块属性取值，
    # 必须 patch 源模块的属性（而不是 qa_router 局部变量）。
    # CG-T1.6 后：Neo4jGraphAdapter 已移除，改走 CodeGraph 路径。
    # db.get 返回 None → repo_local_path=None → codegraph_db_path(None) 会 TypeError；
    # patch codegraph_db_path 源模块属性，让它返回哑路径跳过 os.path.join(None, ...) 崩溃。
    import src.service.qa_engine.tools as _tools_mod
    import src.service.qa_engine.adapters as _adapters
    import src.integrations.codegraph.paths as _cg_paths_mod
    monkeypatch.setattr(_tools_mod, "build_default_registry", _fake_build_default_registry)
    monkeypatch.setattr(_adapters, "WeaviateTopologicalAdapter", lambda store: object())
    # codegraph_db_path 替换成返回固定哑路径（CodeGraphDB.__init__ 不立即打开文件，安全）
    monkeypatch.setattr(_cg_paths_mod, "codegraph_db_path", lambda path: "/dev/null")

    await qa_router.build_tools_for_project("proj-a", _Req(), fake_db)
    assert captured["code_store"] is code_sentinel
    assert captured["method_interp_store"] is interp_sentinel_store


@pytest.mark.asyncio
async def test_build_tools_for_project_passes_project_id_to_registry(monkeypatch):
    """build_tools_for_project 把 URL path 的 project_id 闭包给 ke_search。

    关键不变量：ke_search 收到 LLM 的 input 即使**没**含 project_id 也能正常用闭包查；
    即使 LLM 误传 project_id="wrong" 也被忽略。

    ke_search handler 调用 WeaviateTopologicalAdapter.search_method_hits_by_text；
    这里 monkeypatch WeaviateTopologicalAdapter 使其返回带 spy 的替身，验证 project_id 是闭包值。
    """
    from unittest.mock import MagicMock, AsyncMock
    import src.service.qa_engine.adapters as _adapters

    # spy_adapter：search_method_hits_by_text 有记录功能，不真连 Weaviate
    spy_adapter = MagicMock()
    spy_adapter.search_method_hits_by_text.return_value = []

    # monkeypatch WeaviateTopologicalAdapter 构造，让它直接返回 spy_adapter
    monkeypatch.setattr(
        _adapters, "WeaviateTopologicalAdapter",
        lambda store: spy_adapter,
    )
    # CG-T1.6 后：Neo4jGraphAdapter 已移除，改走 CodeGraph 路径。
    # db.get 返回 None → repo_local_path=None → codegraph_db_path(None) 会 TypeError；
    # patch codegraph_db_path 源模块属性，让它返回哑路径跳过 os.path.join(None, ...) 崩溃。
    import src.integrations.codegraph.paths as _cg_paths_mod
    monkeypatch.setattr(_cg_paths_mod, "codegraph_db_path", lambda path: "/dev/null")

    # 伪造 request.app.state（值是什么不重要，adapter 已被 patch；neo4j_backend 不再必要）
    class _State:
        weaviate_interp_store = object()
        weaviate_code_store = None

    class _App:
        state = _State()

    class _Req:
        app = _App()

    # v2.x：build_tools_for_project 是 async，需要 await + 传 db 参数
    # db.get 返回 None（repo_local_path 降级 None，不影响 ke_search 闭包 project_id 测试）
    fake_db = AsyncMock()
    fake_db.get = AsyncMock(return_value=None)

    registry = await qa_router.build_tools_for_project("mall-swarm", _Req(), fake_db)
    ke_search = registry.get("ke_search")

    # LLM 误传 wrong-tenant 也忽略，用 URL path 闭包的 mall-swarm
    await ke_search.handler({"query": "X", "project_id": "wrong-tenant"})

    spy_adapter.search_method_hits_by_text.assert_called_once()
    call_kwargs = spy_adapter.search_method_hits_by_text.call_args.kwargs
    assert call_kwargs["project_id"] == "mall-swarm"


@pytest.mark.asyncio
async def test_build_retriever_uses_codegraph_graph_adapter(monkeypatch, tmp_path):
    """有 .codegraph.db 时 build_retriever_for_project 构造 CodeGraphGraphAdapter。

    （缺索引时降级为 NullGraphAdapter 的行为见 test_graph_factory_degrade.py。）
    """
    from unittest.mock import AsyncMock, MagicMock
    from src.service.qa_router import build_retriever_for_project
    from src.integrations.codegraph.graph_adapter import CodeGraphGraphAdapter

    # 造一个真实存在的 .codegraph/codegraph.db（resolve_graph_adapter 只看存在性；CodeGraphDB 惰性打开，空文件安全）
    cg = tmp_path / ".codegraph"
    cg.mkdir()
    (cg / "codegraph.db").write_bytes(b"")

    # 伪造 request.app.state：weaviate_interp_store 非 None，不需要 neo4j_backend
    class _State:
        weaviate_interp_store = object()   # 非 None，通过就绪检查
        weaviate_code_store = None         # 可选；None 时 composite 跳过 fallback

    class _App:
        state = _State()

    class _Req:
        app = _App()

    # 伪造 db：db.get(Project, project_id) 返回 repo_local_path 指向上面那个临时目录
    fake_project = MagicMock()
    fake_project.repo_local_path = str(tmp_path)   # 该目录下有 .codegraph/codegraph.db
    fake_db = AsyncMock()
    fake_db.get = AsyncMock(return_value=fake_project)

    # patch WeaviateTopologicalAdapter 避免真连 Weaviate
    import src.service.qa_engine.adapters as _adapters
    monkeypatch.setattr(_adapters, "WeaviateTopologicalAdapter", lambda store: MagicMock())

    retriever = await build_retriever_for_project("p1", _Req(), fake_db)
    # 核心断言：graph 字段是 CodeGraphGraphAdapter 实例（索引存在 → 非降级、非旧 Neo4jGraphAdapter）
    assert isinstance(retriever.graph, CodeGraphGraphAdapter)


@pytest.mark.asyncio
async def test_build_tools_for_project_loads_repo_local_path_from_db():
    """build_tools_for_project 从 DB Project 拿 repo_local_path 闭包给 4 工具。"""
    from unittest.mock import MagicMock, AsyncMock
    from fastapi import Request
    from src.service.qa_router import build_tools_for_project

    # 假 Project 对象
    fake_project = MagicMock()
    fake_project.repo_local_path = "/tmp/fake-repo"

    # mock app.state（CG-T1.6 后：结构导航走 CodeGraph，neo4j_backend 不再必要）
    request = MagicMock(spec=Request)
    request.app.state.weaviate_interp_store = MagicMock()
    request.app.state.weaviate_code_store = None

    # mock db.get(Project, ...)
    fake_db = AsyncMock()
    fake_db.get = AsyncMock(return_value=fake_project)

    registry = await build_tools_for_project("mall-swarm", request, fake_db)
    # 4 工具都注册了
    for name in ("ke_grep", "ke_glob", "ke_read_file", "ke_ls"):
        assert registry.get(name) is not None


@pytest.mark.asyncio
async def test_build_retriever_injects_recall_threshold(monkeypatch, tmp_path):
    """build_retriever_for_project 把 env KE_QA_RECALL_THRESHOLD 注入 QARetriever.recall_threshold；缺省 0.45。"""
    from unittest.mock import AsyncMock, MagicMock
    from src.service.qa_router import build_retriever_for_project
    import src.service.qa_engine.adapters as _adapters

    # 造真实存在的 .codegraph.db → resolve_graph_adapter 给真 adapter（懒打开）
    cg = tmp_path / ".codegraph"; cg.mkdir(); (cg / "codegraph.db").write_bytes(b"")

    # 伪造 request.app.state：weaviate_interp_store 非 None，通过就绪检查
    class _State:
        weaviate_interp_store = object()  # 非 None，通过就绪检查
        weaviate_code_store = None        # 可选；None 时 composite 跳过 fallback

    class _Req:
        # type(name, bases, dict) 动态建类：A 继承 object，含 state 属性
        app = type("A", (), {"state": _State()})()

    # 伪造 db：db.get(Project, project_id) 返回带 repo_local_path 的假对象
    fake_project = MagicMock(); fake_project.repo_local_path = str(tmp_path)
    fake_db = AsyncMock(); fake_db.get = AsyncMock(return_value=fake_project)

    # patch WeaviateTopologicalAdapter 避免真连 Weaviate
    monkeypatch.setattr(_adapters, "WeaviateTopologicalAdapter", lambda store: MagicMock())

    # 场景1：env 设为 0.6 → recall_threshold 应为 0.6
    monkeypatch.setenv("KE_QA_RECALL_THRESHOLD", "0.6")
    r = await build_retriever_for_project("mall-swarm", _Req(), fake_db)
    assert r.recall_threshold == 0.6

    # 场景2：env 不存在 → recall_threshold 应为缺省值 0.45
    monkeypatch.delenv("KE_QA_RECALL_THRESHOLD", raising=False)
    r2 = await build_retriever_for_project("mall-swarm", _Req(), fake_db)
    assert r2.recall_threshold == 0.45
