"""ke_callees 工具：图谱下游 1 跳（同 retriever 的 callees_by_entry 但作为可独立调用的 Tool 暴露）。

未来场景：
  - LLM 在 ReAct 模式下：发现 entry_point 不够具体 → 主动 call ke_callees(entry_point)
  - 外部 MCP client：列工具时看到 ke_callees，按 JSONSchema 调用
"""
from __future__ import annotations

from typing import Any

# 注入式 GraphProto；ke_callees 不直接 import Neo4jGraphBackend，
# 这样 tools 包零依赖于主仓 src.knowledge.* 实现细节
from src.service.qa_engine.retriever import GraphProto
from src.service.qa_engine.tools.base import Tool


# input_schema：JSONSchema，对接 MCP 客户端时直接 JSON 序列化扔过去
# `required` 数组列出必填字段；非必填字段在 properties 里仍可声明
_KE_CALLEES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entity_id": {
            "type": "string",
            "description": "实体 ID，形如 OmsPortalOrderServiceImpl::generateOrder#(OrderParam)（qualified_name 形态）",
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


# 工厂函数：把 graph 实例 "闭包" 进 handler，handler 自身不依赖外部状态
# 这种"工厂返回 closure"是 Python 注入式编程的常用模式（替代构造类）
def build_ke_callees_tool(graph: GraphProto) -> Tool:
    """构造一个绑定到指定 GraphProto 实例的 ke_callees Tool。

    :param graph: GraphProto 实现（Neo4jGraphAdapter / 单测 mock）
    :return: 可注册到 ToolRegistry 的 Tool 实例
    """

    # handler 是 async function；嵌套定义时它捕获外层的 graph 变量（closure）
    async def handler(input: dict[str, Any]) -> dict[str, Any]:
        # 校验必填：没 entity_id → 返回错误信号给 LLM 自己重试
        entity_id = input.get("entity_id")
        if not entity_id:
            return {
                "entity_id": None,
                "callees": [],
                "error": "missing required field: entity_id",
            }

        # max_nodes 默认 10；类型容错（LLM 可能传 string "5"）
        try:
            max_nodes = int(input.get("max_nodes", 10))
        except (TypeError, ValueError):
            max_nodes = 10

        # 调底层 graph；GraphProto.successors 是同步函数，async handler 直接调即可
        try:
            callees = list(graph.successors(entity_id))[:max_nodes]
        except Exception as e:
            # 图谱后端挂了 → 返回错误信号；调用方可重试 / 兜底
            return {
                "entity_id": entity_id,
                "callees": [],
                "error": f"graph backend error: {e}",
            }

        return {"entity_id": entity_id, "callees": callees}

    # 把 handler 和元数据打包成 Tool
    return Tool(
        name="ke_callees",
        description="查询某个代码实体（method / class）的下游调用 —— 它调用了哪些方法 / 类。",
        input_schema=_KE_CALLEES_SCHEMA,
        handler=handler,
    )
