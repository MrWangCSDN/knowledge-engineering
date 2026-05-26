"""MCP-style 工具框架：6 个 ke_* 核心工具 + todo_write 元工具 + Registry + 默认装配工厂。

设计文档：[[首页设计]] §13 v1.2

公开 API:
  - Tool / ToolRegistry / ToolNotFound      from .base
  - build_default_registry(*, graph, business_store, project_id, code_store=None, method_interp_store=None) -> ToolRegistry
  - 6 个 build_ke_xxx_tool 工厂函数 + build_todo_write_tool（生产用 build_default_registry 一把推；测试可单挑）
"""
from __future__ import annotations

from typing import Any

from src.service.qa_engine.tools.base import Tool, ToolNotFound, ToolRegistry
from src.service.qa_engine.tools.ke_business_interp import build_ke_business_interp_tool
from src.service.qa_engine.tools.ke_callees import build_ke_callees_tool
from src.service.qa_engine.tools.ke_callers import build_ke_callers_tool
from src.service.qa_engine.tools.ke_search import build_ke_search_tool
from src.service.qa_engine.tools.ke_table_access import build_ke_table_access_tool
from src.service.qa_engine.tools.ke_impact import build_ke_impact_tool
from src.service.qa_engine.tools.ke_read_entity import build_ke_read_entity_tool
from src.service.qa_engine.tools.ke_method_interp import build_ke_method_interp_tool
from src.service.qa_engine.tools.todo_write import build_todo_write_tool
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
    "build_ke_impact_tool",
    "build_ke_read_entity_tool",
    "build_ke_method_interp_tool",
    "build_todo_write_tool",
    "build_default_registry",
]


def build_default_registry(
    *,
    graph: GraphProto,
    business_store: BusinessStoreProto,
    project_id: str,
    code_store: Any | None = None,
    method_interp_store: Any | None = None,
) -> ToolRegistry:
    """构造预装 ke_* 工具 + todo_write 元工具的 ToolRegistry。

    v1.3 修复（2026-05-26）：新增必填 project_id，闭包透传给 ke_search（修 LLM 猜 tenant bug）。
    其他 ke_* 工具的 project_id 由 graph adapter（如 Neo4jGraphAdapter）实例化时绑定，
    本函数不直接经手。

    本函数构造 6 个核心 ke_* 工具（ke_search / ke_business_interp / ke_callees / ke_callers /
    ke_table_access / ke_impact）+ todo_write 元工具（设计 §3.3，无后端依赖始终注册）+ 2 个可选
    ke_* 工具（ke_read_entity / ke_method_interp，分别依赖 code_store / method_interp_store，
    None 时优雅缺省）。

    :param graph: 已绑 project_id 的 GraphProto adapter（如 Neo4jGraphAdapter(..., project_id=...)）
    :param business_store: BusinessInterpretation store 的 adapter
    :param project_id: 当前请求工程 ID — 透传给 ke_search 闭包
    :param code_store: 可选 CodeEntity store
    :param method_interp_store: 可选 MethodInterpretation store
    """
    registry = ToolRegistry()
    # ke_search 闭包绑 project_id（与 Neo4jGraphAdapter 设计对齐）
    registry.register(build_ke_search_tool(business_store, project_id))
    registry.register(build_ke_business_interp_tool(business_store))
    registry.register(build_ke_callees_tool(graph))
    registry.register(build_ke_callers_tool(graph))
    registry.register(build_ke_table_access_tool(graph))
    registry.register(build_ke_impact_tool(graph))
    # meta 工具：todo_write 无后端依赖，始终注册（设计 §3.3）
    registry.register(build_todo_write_tool())
    if code_store is not None:
        registry.register(build_ke_read_entity_tool(code_store))
    if method_interp_store is not None:
        registry.register(build_ke_method_interp_tool(method_interp_store))
    return registry
