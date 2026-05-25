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
