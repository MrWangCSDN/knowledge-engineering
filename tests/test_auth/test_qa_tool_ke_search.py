"""ke_search 工具闭包注入 project_id 行为测试。

修复前：schema required=["query","project_id"]，handler 从 input.get("project_id") 取，LLM 猜对就用，猜错就查不到。
修复后：schema 只 required=["query"]，handler 从 build_ke_search_tool(store, project_id) 闭包注入。
"""
import asyncio
import pytest
from unittest.mock import MagicMock

from src.service.qa_engine.tools.ke_search import build_ke_search_tool


def _run(coro):
    """同步跑 async — pytest-asyncio 没启用时用这个 helper。"""
    return asyncio.get_event_loop().run_until_complete(coro)


def test_ke_search_schema_drops_project_id():
    """schema 不再 require project_id（修 LLM 猜 tenant 的 bug）。"""
    store = MagicMock()
    tool = build_ke_search_tool(store, project_id="mall-swarm")
    assert tool.input_schema["required"] == ["query"]
    assert "project_id" not in tool.input_schema["properties"]


def test_ke_search_handler_uses_closure_project_id():
    """handler 用 builder 闭包的 project_id，不读 input dict。"""
    store = MagicMock()
    store.search_method_hits_by_text.return_value = [{"entity_id": "method//abc", "score": 0.9}]

    tool = build_ke_search_tool(store, project_id="mall-swarm")
    result = _run(tool.handler({"query": "OrderService", "project_id": "wrong-tenant"}))

    store.search_method_hits_by_text.assert_called_once_with(
        text="OrderService", project_id="mall-swarm", limit=5
    )
    assert result["results"][0]["entity_id"] == "method//abc"


def test_ke_search_handler_missing_query_returns_error():
    """缺 query 立即返回错误（不调 store）。"""
    store = MagicMock()
    tool = build_ke_search_tool(store, project_id="mall-swarm")
    result = _run(tool.handler({"query": ""}))
    assert "error" in result
    assert result["results"] == []
    store.search_method_hits_by_text.assert_not_called()


def test_ke_search_builder_rejects_empty_project_id():
    """builder 阶段就拒绝空 project_id（避免运行时 with_tenant('') 的隐蔽 bug）。"""
    store = MagicMock()
    with pytest.raises(ValueError, match="project_id"):
        build_ke_search_tool(store, project_id="")
