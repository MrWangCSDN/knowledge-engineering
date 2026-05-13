"""ke_business_interp 工具：按 entity_id 精确查业务解读完整记录。

跟 ke_search（模糊搜）互补：知道 entity_id 后想看它完整的 business_domain /
business_capabilities / summary_text 全文 → 用这个。
"""
from __future__ import annotations

from typing import Any

from src.service.qa_engine.retriever import BusinessStoreProto
from src.service.qa_engine.tools.base import Tool


_KE_BUSINESS_INTERP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entity_id": {
            "type": "string",
            "description": "实体 ID（形如 method//xxx / class//xxx / module//xxx）",
        },
        "level": {
            "type": "string",
            "description": "可选过滤 'api' / 'method' / 'class' / 'module'",
            "enum": ["api", "method", "class", "module"],
        },
    },
    "required": ["entity_id"],
}


def build_ke_business_interp_tool(store: BusinessStoreProto) -> Tool:
    """构造一个绑定到指定 BusinessStoreProto 的 ke_business_interp Tool。"""

    async def handler(input: dict[str, Any]) -> dict[str, Any]:
        # 必填校验
        entity_id = (input.get("entity_id") or "").strip()
        if not entity_id:
            return {
                "interpretation": None,
                "error": "missing required field: entity_id",
            }
        # level 可选；空字符串视作 None（让 store 不做 level 过滤）
        level_raw = input.get("level")
        level = level_raw.strip() if isinstance(level_raw, str) and level_raw.strip() else None

        try:
            record = store.get_by_entity(entity_id, level=level)
        except Exception as e:
            return {
                "interpretation": None,
                "error": f"backend error: {e}",
            }

        # 找不到 → 显式返回 None + error 字段（让 LLM 区分『没找到』vs『拿到空字段』）
        if record is None:
            return {
                "interpretation": None,
                "error": f"not found: entity_id={entity_id!r}, level={level!r}",
            }

        return {"interpretation": record}

    return Tool(
        name="ke_business_interp",
        description="按 entity_id 精确查一条业务解读（含 business_domain / capabilities / 完整 summary）。",
        input_schema=_KE_BUSINESS_INTERP_SCHEMA,
        handler=handler,
    )
