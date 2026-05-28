"""验证 build_default_registry()：一句话推出 6 个内置工具。

让 api.py startup 可以这样写：
  app.state.qa_tools = build_default_registry(graph=..., business_store=...)
不用手动列 6 个。
"""
from unittest.mock import MagicMock

import pytest

# 这个 helper 会汇总 6 个 ke_* 工具到 ToolRegistry
from src.service.qa_engine.tools import build_default_registry


def test_default_registry_has_core_tools_plus_todo_write() -> None:
    """build_default_registry 把核心 ke_* + todo_write 元工具 + 4 个文件类工具全注册进去。

    Task 3：ke_business_interp 已删除，核心工具集为 5 个（无 ke_business_interp）。
    """
    graph = MagicMock()
    store = MagicMock()
    reg = build_default_registry(graph=graph, interpretation_store=store, project_id="test")

    names = {t.name for t in reg.list_tools()}
    assert names == {
        "ke_search",
        "ke_callees",
        "ke_callers",
        "ke_table_access",
        "ke_impact",
        "todo_write",
        "ke_grep",
        "ke_glob",
        "ke_read_file",
        "ke_ls",
    }


@pytest.mark.asyncio
async def test_default_registry_call_dispatches() -> None:
    """通过 registry.call('ke_callees', input) 能调通底层 graph.successors。"""
    graph = MagicMock()
    graph.successors.return_value = ["B"]
    store = MagicMock()

    reg = build_default_registry(graph=graph, interpretation_store=store, project_id="test")
    out = await reg.call("ke_callees", {"entity_id": "A"})

    assert out == {"entity_id": "A", "callees": ["B"]}


def test_default_registry_registers_optional_stores_when_provided():
    """传入 code_store / method_interp_store 时，注册 ke_read_entity / ke_method_interp。"""
    from src.service.qa_engine.tools import build_default_registry

    class _FakeGraph:
        def successors(self, e, rel_type=None): return []
        def predecessors(self, e, rel_type=None): return []

    class _FakeBiz:
        def get_by_entity(self, entity_id, *, project_id, level=None): return None
        def search_method_hits_by_text(self, *, text, project_id, limit=5): return []

    class _FakeCodeStore:
        def get_by_entity_id(self, entity_id): return None

    class _FakeInterpStore:
        def get_by_method_id(self, method_entity_id): return None

    reg = build_default_registry(
        graph=_FakeGraph(),
        interpretation_store=_FakeBiz(),
        project_id="test",
        code_store=_FakeCodeStore(),
        method_interp_store=_FakeInterpStore(),
    )
    names = {t.name for t in reg.list_tools()}
    assert "ke_read_entity" in names
    assert "ke_method_interp" in names


def test_default_registry_skips_optional_stores_when_absent():
    """不传 code_store / method_interp_store 时，不注册这两个工具（向后兼容）。"""
    from src.service.qa_engine.tools import build_default_registry

    class _FakeGraph:
        def successors(self, e, rel_type=None): return []
        def predecessors(self, e, rel_type=None): return []

    class _FakeBiz:
        def get_by_entity(self, entity_id, *, project_id, level=None): return None
        def search_method_hits_by_text(self, *, text, project_id, limit=5): return []

    reg = build_default_registry(graph=_FakeGraph(), interpretation_store=_FakeBiz(), project_id="test")
    names = {t.name for t in reg.list_tools()}
    assert "ke_read_entity" not in names
    assert "ke_method_interp" not in names


def test_build_default_registry_requires_project_id():
    """build_default_registry 现在必填 project_id（透传给 ke_search 闭包）。"""
    import pytest
    from unittest.mock import MagicMock
    from src.service.qa_engine.tools import build_default_registry

    graph = MagicMock()
    business = MagicMock()
    # 不传 project_id 应当报错（旧 signature 兼容性破坏，意在强制升级调用方）
    with pytest.raises(TypeError):
        build_default_registry(graph=graph, interpretation_store=business)  # type: ignore[call-arg]


def test_build_default_registry_passes_project_id_to_ke_search():
    """build_default_registry 把 project_id 闭包传到 ke_search。"""
    from unittest.mock import MagicMock
    from src.service.qa_engine.tools import build_default_registry

    graph = MagicMock()
    business = MagicMock()
    business.search_method_hits_by_text.return_value = []

    registry = build_default_registry(
        graph=graph, interpretation_store=business, project_id="mall-swarm"
    )
    # 拿 ke_search 工具并调一次 handler，验证 project_id 闭包到位
    import asyncio
    tool = registry.get("ke_search")
    # asyncio.run 替代 get_event_loop().run_until_complete()，避免 Python 3.12 full-suite RuntimeError
    asyncio.run(tool.handler({"query": "X"}))
    business.search_method_hits_by_text.assert_called_once_with(
        text="X", project_id="mall-swarm", limit=5
    )


def test_build_default_registry_accepts_repo_local_path():
    """build_default_registry 接受 repo_local_path 参数并注册 4 个文件类工具。"""
    from unittest.mock import MagicMock
    from src.service.qa_engine.tools import build_default_registry

    graph = MagicMock()
    business = MagicMock()
    registry = build_default_registry(
        graph=graph,
        interpretation_store=business,
        project_id="test",
        repo_local_path="/tmp/fake-repo",
    )

    # 4 个新工具被注册
    for name in ("ke_grep", "ke_glob", "ke_read_file", "ke_ls"):
        assert registry.get(name) is not None, f"{name} not registered"


def test_build_default_registry_without_repo_local_path():
    """repo_local_path 为 None 时 4 个工具仍注册（handler 内部 error 处理）。"""
    from unittest.mock import MagicMock
    from src.service.qa_engine.tools import build_default_registry

    graph = MagicMock()
    business = MagicMock()
    registry = build_default_registry(
        graph=graph,
        interpretation_store=business,
        project_id="test",
        # 不传 repo_local_path
    )

    # 4 个工具仍注册
    for name in ("ke_grep", "ke_glob", "ke_read_file", "ke_ls"):
        assert registry.get(name) is not None
