"""MCP-style 工具框架：5 个 ke_* 工具 + Registry + 默认装配工厂。

设计文档：[[首页设计]] §13 v1.2

公开 API:
  - Tool / ToolRegistry / ToolNotFound      from .base
  - build_default_registry(graph, business_store) -> ToolRegistry
  - 5 个 build_ke_xxx_tool 工厂函数（生产用 build_default_registry 一把推；测试可单挑）
"""
from __future__ import annotations

from src.service.qa_engine.tools.base import Tool, ToolNotFound, ToolRegistry
from src.service.qa_engine.tools.ke_business_interp import build_ke_business_interp_tool
from src.service.qa_engine.tools.ke_callees import build_ke_callees_tool
from src.service.qa_engine.tools.ke_callers import build_ke_callers_tool
from src.service.qa_engine.tools.ke_search import build_ke_search_tool
from src.service.qa_engine.tools.ke_table_access import build_ke_table_access_tool
from src.service.qa_engine.retriever import BusinessStoreProto, GraphProto


# `__all__` 显式暴露公开 API；用 import * 时只会导入这些名字
__all__ = [
    "Tool",
    "ToolRegistry",
    "ToolNotFound",
    "build_ke_callees_tool",
    "build_ke_callers_tool",
    "build_ke_search_tool",
    "build_ke_business_interp_tool",
    "build_ke_table_access_tool",
    "build_default_registry",
]


def build_default_registry(
    *,
    graph: GraphProto,
    business_store: BusinessStoreProto,
) -> ToolRegistry:
    """构造预装了 5 个 ke_* 工具的 ToolRegistry。

    api.py startup 用：
        app.state.qa_tools = build_default_registry(
            graph=neo4j_adapter,
            business_store=weaviate_adapter,
        )

    :param graph: GraphProto 实现（Neo4jGraphAdapter / 测试 mock）
    :param business_store: BusinessStoreProto 实现（WeaviateBusinessAdapter / 测试 mock）
    :return: 包含 5 个工具的 ToolRegistry，可立即使用
    """
    registry = ToolRegistry()
    # 注册顺序 = list_tools() 输出顺序；这里按"用户最常用先"排
    registry.register(build_ke_search_tool(business_store))
    registry.register(build_ke_business_interp_tool(business_store))
    registry.register(build_ke_callees_tool(graph))
    registry.register(build_ke_callers_tool(graph))
    registry.register(build_ke_table_access_tool(graph))
    return registry
