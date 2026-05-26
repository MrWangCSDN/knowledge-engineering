"""ke_search 工具：BusinessInterpretation 向量库语义检索。

v1.3 修复（2026-05-26）：schema 去掉 project_id 必填；project_id 改由
build_ke_search_tool(store, project_id) 闭包注入，与 Neo4jGraphAdapter 的设计对齐。
原因：让 LLM 自己猜 project_id 会出错（mall-swarm 实测被猜成 "pms"/"pms-product"
→ tenant 不匹配 → 0 结果）。
"""
from __future__ import annotations

from typing import Any

from src.service.qa_engine.retriever import BusinessStoreProto
from src.service.qa_engine.tools.base import Tool


# schema：只 require query；project_id 走闭包不暴露给 LLM
_KE_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "自然语言查询（中文优先）",
        },
        "limit": {
            "type": "integer",
            "description": "返回结果上限",
            "default": 5,
            "minimum": 1,
            "maximum": 50,
        },
    },
    "required": ["query"],
}


def build_ke_search_tool(store: BusinessStoreProto, project_id: str) -> Tool:
    """构造一个绑定到指定 store + project_id 的 ke_search Tool。

    :param store: BusinessInterpretation 向量库 store（实现 BusinessStoreProto）
    :param project_id: 当前请求的工程 ID（Weaviate tenant 标识）。
        必须非空，由 build_tools_for_project 从 URL path 传入。
    :raises ValueError: project_id 为空字符串。
    """
    if not project_id or not project_id.strip():
        raise ValueError("build_ke_search_tool: project_id 不能为空")
    bound_project_id = project_id.strip()

    async def handler(input: dict[str, Any]) -> dict[str, Any]:
        query = (input.get("query") or "").strip()
        if not query:
            return {
                "query": query,
                "results": [],
                "error": "missing required field: query",
            }

        try:
            limit = int(input.get("limit", 5))
        except (TypeError, ValueError):
            limit = 5

        try:
            results = store.search_method_hits_by_text(
                text=query, project_id=bound_project_id, limit=limit
            )
        except Exception as e:
            return {
                "query": query,
                "results": [],
                "error": f"search backend error: {e}",
            }

        return {"query": query, "results": list(results)}

    return Tool(
        name="ke_search",
        description="在 BusinessInterpretation 向量库语义检索代码实体（method / class / module / api）；project_id 已由后端绑定，无需提供。",
        input_schema=_KE_SEARCH_SCHEMA,
        handler=handler,
    )
