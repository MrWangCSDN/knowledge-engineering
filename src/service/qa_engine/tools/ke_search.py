"""ke_search 工具：BusinessInterpretation 向量库语义检索。

跟 QARetriever 内部用的 store.search_method_hits_by_text 同一个底层，
但暴露为独立工具便于 LLM / 外部 client 单独调用。
"""
from __future__ import annotations

from typing import Any

from src.service.qa_engine.retriever import BusinessStoreProto
from src.service.qa_engine.tools.base import Tool


_KE_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "自然语言查询（中文优先）",
        },
        "project_id": {
            "type": "string",
            "description": "工程 ID，多工程隔离用",
        },
        "limit": {
            "type": "integer",
            "description": "返回结果上限",
            "default": 5,
            "minimum": 1,
            "maximum": 50,
        },
    },
    "required": ["query", "project_id"],
}


def build_ke_search_tool(store: BusinessStoreProto) -> Tool:
    """构造一个绑定到指定 BusinessStoreProto 的 ke_search Tool。"""

    async def handler(input: dict[str, Any]) -> dict[str, Any]:
        # `or "" + .strip()`：兼容 query 为 None 或 空白字符串两种情况
        query = (input.get("query") or "").strip()
        project_id = (input.get("project_id") or "").strip()

        # 任一关键参数缺失就拒绝
        if not query or not project_id:
            return {
                "query": query,
                "results": [],
                "error": "missing required field: query and/or project_id",
            }

        try:
            limit = int(input.get("limit", 5))
        except (TypeError, ValueError):
            limit = 5

        # 调底层 store；store 用 Protocol 注入，单测 / 生产用同一个接口
        try:
            results = store.search_method_hits_by_text(
                text=query, project_id=project_id, limit=limit
            )
        except Exception as e:
            return {
                "query": query,
                "results": [],
                "error": f"search backend error: {e}",
            }

        # results 已经是 list[dict]，原样透传
        # 不在 tool 里做 reranking / 过滤，让上层（skill 或 LLM）决定
        return {"query": query, "results": list(results)}

    return Tool(
        name="ke_search",
        description="在 BusinessInterpretation 向量库语义检索代码实体（method / class / module / api）。",
        input_schema=_KE_SEARCH_SCHEMA,
        handler=handler,
    )
