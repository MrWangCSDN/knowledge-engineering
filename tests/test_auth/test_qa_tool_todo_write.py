"""todo_write 元工具单测（设计 §3.3）：纯回显 items、无后端依赖。"""
import pytest

from src.service.qa_engine.tools.todo_write import build_todo_write_tool


def test_build_todo_write_tool_metadata():
    """工具名 / schema 基本契约。"""
    tool = build_todo_write_tool()
    assert tool.name == "todo_write"
    assert tool.input_schema["required"] == ["items"]
    assert tool.input_schema["properties"]["items"]["type"] == "array"


@pytest.mark.asyncio
async def test_todo_write_handler_echoes_items():
    """handler 纯回显传入的 items（不查后端）。"""
    tool = build_todo_write_tool()
    items = [
        {"content": "分析订单域入口", "status": "in_progress"},
        {"content": "画调用链", "status": "pending"},
    ]
    out = await tool.handler({"items": items})
    assert out["items"] == items
    assert out["count"] == 2


@pytest.mark.asyncio
async def test_todo_write_handler_missing_items_defaults_empty():
    """items 缺失 / 非 list 时兜底空列表，不抛（§3.4 信号哲学）。"""
    tool = build_todo_write_tool()
    assert (await tool.handler({}))["items"] == []
    assert (await tool.handler({"items": "oops"}))["items"] == []
    assert (await tool.handler({"items": None}))["items"] == []
