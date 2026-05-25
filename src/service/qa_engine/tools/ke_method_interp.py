"""ke_method_interp 工具：按 method entity_id 读方法级技术解读（Mode A）。

从 Weaviate MethodInterpretation store 取完整解读记录。
鸭子类型注入：store 只需有 get_by_method_id(method_entity_id) -> dict | None。

⚠️ 多租户：store 全局按 method_entity_id 查，无 project_id 隔离（见设计 §4.2 缺口）。
"""
from __future__ import annotations

from typing import Any, Optional, Protocol

from src.service.qa_engine.tools.base import Tool


class _MethodInterpStoreProto(Protocol):
    """ke_method_interp 依赖的最小 store 接口。"""
    def get_by_method_id(self, method_entity_id: str) -> Optional[dict[str, Any]]: ...


_KE_METHOD_INTERP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entity_id": {
            "type": "string",
            "description": "方法实体 ID，形如 method//xxx",
        },
    },
    "required": ["entity_id"],
}


def build_ke_method_interp_tool(interp_store: _MethodInterpStoreProto) -> Tool:
    """构造绑定到 MethodInterpretation store 的 ke_method_interp Tool。"""

    async def handler(input: dict[str, Any]) -> dict[str, Any]:
        entity_id = input.get("entity_id")
        if not entity_id:
            return {
                "entity_id": None,
                "interpretation": None,
                "error": "missing required field: entity_id",
            }
        try:
            record = interp_store.get_by_method_id(entity_id)
        except Exception as e:
            return {
                "entity_id": entity_id,
                "interpretation": None,
                "error": f"method interp store error: {e}",
            }
        if record is None:
            return {
                "entity_id": entity_id,
                "interpretation": None,
                "error": "method interpretation not found",
            }
        return {"entity_id": entity_id, "interpretation": record}

    return Tool(
        name="ke_method_interp",
        description="读取某个方法（method//xxx）的技术解读：它做什么、关键逻辑、上下文。",
        input_schema=_KE_METHOD_INTERP_SCHEMA,
        handler=handler,
    )
