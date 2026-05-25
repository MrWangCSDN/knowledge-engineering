"""验证 build_default_registry()：一句话推出 6 个内置工具。

让 api.py startup 可以这样写：
  app.state.qa_tools = build_default_registry(graph=..., business_store=...)
不用手动列 6 个。
"""
from unittest.mock import MagicMock

import pytest

# 这个 helper 会汇总 6 个 ke_* 工具到 ToolRegistry
from src.service.qa_engine.tools import build_default_registry


def test_default_registry_has_all_six_tools() -> None:
    """build_default_registry 把 6 个 ke_* 全注册进去。"""
    graph = MagicMock()
    store = MagicMock()
    reg = build_default_registry(graph=graph, business_store=store)

    names = {t.name for t in reg.list_tools()}
    assert names == {
        "ke_search",
        "ke_callees",
        "ke_callers",
        "ke_business_interp",
        "ke_table_access",
        "ke_impact",
    }


@pytest.mark.asyncio
async def test_default_registry_call_dispatches() -> None:
    """通过 registry.call('ke_callees', input) 能调通底层 graph.successors。"""
    graph = MagicMock()
    graph.successors.return_value = ["B"]
    store = MagicMock()

    reg = build_default_registry(graph=graph, business_store=store)
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
        business_store=_FakeBiz(),
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

    reg = build_default_registry(graph=_FakeGraph(), business_store=_FakeBiz())
    names = {t.name for t in reg.list_tools()}
    assert "ke_read_entity" not in names
    assert "ke_method_interp" not in names
