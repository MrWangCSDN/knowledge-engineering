"""ke_callers 工具：图谱上游 1 跳（谁调用了这个实体）。

跟 ke_callees 对称；为了让外部 MCP 客户端能分别调用上游 / 下游而拆成两个工具。
"""
from __future__ import annotations

from typing import Any

from src.service.qa_engine.retriever import GraphProto
from src.service.qa_engine.tools.base import Tool


_KE_CALLERS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entity_id": {
            "type": "string",
            "description": "实体 ID，形如 method//xxx 或 class//xxx",
        },
        "max_nodes": {
            "type": "integer",
            "description": "返回结果上限",
            "default": 10,
            "minimum": 1,
            "maximum": 100,
        },
    },
    "required": ["entity_id"],
}


def build_ke_callers_tool(graph: GraphProto) -> Tool:
    """构造一个绑定到指定 GraphProto 实例的 ke_callers Tool。"""

    async def handler(input: dict[str, Any]) -> dict[str, Any]:
        # 没 entity_id 就返回错误信号；不抛
        entity_id = input.get("entity_id")
        if not entity_id:
            return {
                "entity_id": None,
                "callers": [],
                "error": "missing required field: entity_id",
            }

        # max_nodes 容错
        try:
            max_nodes = int(input.get("max_nodes", 10))
        except (TypeError, ValueError):
            max_nodes = 10

        # `predecessors` 拿上游（谁调用了 entity_id）
        try:
            callers = list(graph.predecessors(entity_id))[:max_nodes]
        except Exception as e:
            return {
                "entity_id": entity_id,
                "callers": [],
                "error": f"graph backend error: {e}",
            }

        return {"entity_id": entity_id, "callers": callers}

    return Tool(
        name="ke_callers",
        description="查询某个代码实体（method / class）的上游调用 —— 谁调用了它。",
        input_schema=_KE_CALLERS_SCHEMA,
        handler=handler,
    )
