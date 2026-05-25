"""ke_read_entity 工具：按 entity_id 读源代码片段（代码片段展示能力）。

从 Weaviate CodeEntity store 取 {name, entity_type, code_snippet}。
鸭子类型注入：store 只需有 get_by_entity_id(entity_id) -> dict | None。

⚠️ 多租户：CodeEntity store 全局按 entity_id 查，无 project_id 隔离（见设计 §4.2 缺口）。
"""
from __future__ import annotations

from typing import Any, Optional, Protocol

from src.service.qa_engine.tools.base import Tool


class _CodeStoreProto(Protocol):
    """ke_read_entity 依赖的最小 store 接口。"""
    def get_by_entity_id(self, entity_id: str) -> Optional[dict[str, Any]]: ...


_KE_READ_ENTITY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entity_id": {
            "type": "string",
            "description": "实体 ID，形如 method//xxx 或 class//xxx",
        },
    },
    "required": ["entity_id"],
}


def build_ke_read_entity_tool(code_store: _CodeStoreProto) -> Tool:
    """构造绑定到 CodeEntity store 的 ke_read_entity Tool。"""

    async def handler(input: dict[str, Any]) -> dict[str, Any]:
        entity_id = input.get("entity_id")
        if not entity_id:
            return {
                "entity_id": None,
                "name": None,
                "entity_type": None,
                "code_snippet": None,
                "error": "missing required field: entity_id",
            }
        try:
            record = code_store.get_by_entity_id(entity_id)
        except Exception as e:
            return {
                "entity_id": entity_id,
                "name": None,
                "entity_type": None,
                "code_snippet": None,
                "error": f"code store error: {e}",
            }
        if record is None:
            return {
                "entity_id": entity_id,
                "name": None,
                "entity_type": None,
                "code_snippet": None,
                "error": "entity not found in code store",
            }
        return {
            "entity_id": entity_id,
            "name": record.get("name"),
            "entity_type": record.get("entity_type"),
            "code_snippet": record.get("code_snippet"),
        }

    return Tool(
        name="ke_read_entity",
        description="读取某个代码实体（method/class）的源代码片段、名称与类型。",
        input_schema=_KE_READ_ENTITY_SCHEMA,
        handler=handler,
    )
