"""MCP-style 工具框架：ke_* 核心工具 + todo_write 元工具 + 4 个文件类工具 + Registry + 默认装配工厂。

设计文档：[[首页设计]] §13 v1.2；[[代码源文件查询工具-设计]] §3

公开 API:
  - Tool / ToolRegistry / ToolNotFound      from .base
  - build_default_registry(*, graph, interpretation_store, project_id, code_store=None,
      method_interp_store=None, repo_local_path=None) -> ToolRegistry
  - build_ke_xxx_tool 工厂函数 + build_todo_write_tool（生产用 build_default_registry 一把推；测试可单挑）
  - 4 个文件类工具工厂：build_ke_grep_tool / build_ke_glob_tool / build_ke_read_file_tool / build_ke_ls_tool
"""
from __future__ import annotations

from typing import Any

from src.service.qa_engine.tools.base import Tool, ToolNotFound, ToolRegistry
from src.service.qa_engine.tools.ke_callees import build_ke_callees_tool
from src.service.qa_engine.tools.ke_callers import build_ke_callers_tool
from src.service.qa_engine.tools.ke_search import build_ke_search_tool
from src.service.qa_engine.tools.ke_table_access import build_ke_table_access_tool
from src.service.qa_engine.tools.ke_impact import build_ke_impact_tool
from src.service.qa_engine.tools.ke_read_entity import build_ke_read_entity_tool
from src.service.qa_engine.tools.ke_method_interp import build_ke_method_interp_tool
from src.service.qa_engine.tools.todo_write import build_todo_write_tool
from src.service.qa_engine.tools.ke_grep import build_ke_grep_tool
from src.service.qa_engine.tools.ke_glob import build_ke_glob_tool
from src.service.qa_engine.tools.ke_read_file import build_ke_read_file_tool
from src.service.qa_engine.tools.ke_ls import build_ke_ls_tool
from src.service.qa_engine.tools.render_call_graph import build_render_call_graph_tool
from src.service.qa_engine.retriever import InterpretationStoreProto, GraphProto


# `__all__` 显式暴露公开 API；用 import * 时只会导入这些名字
__all__ = [
    "Tool",
    "ToolRegistry",
    "ToolNotFound",
    "build_ke_callees_tool",
    "build_ke_callers_tool",
    "build_ke_search_tool",
    "build_ke_table_access_tool",
    "build_ke_impact_tool",
    "build_ke_read_entity_tool",
    "build_ke_method_interp_tool",
    "build_todo_write_tool",
    "build_ke_grep_tool",
    "build_ke_glob_tool",
    "build_ke_read_file_tool",
    "build_ke_ls_tool",
    "build_render_call_graph_tool",
    "build_default_registry",
]


def build_default_registry(
    *,
    graph: GraphProto,
    interpretation_store: InterpretationStoreProto,
    project_id: str,
    code_store: Any | None = None,
    method_interp_store: Any | None = None,
    repo_local_path: str | None = None,
) -> ToolRegistry:
    """构造预装 ke_* 工具 + todo_write 元工具 + 4 个文件类工具的 ToolRegistry。

    v1.3 修复（2026-05-26）：新增必填 project_id，闭包透传给 ke_search（修 LLM 猜 tenant bug）。
    v1.4（2026-05-27）：新增 repo_local_path，注册 4 个文件类工具。
    Task 3（2026-05-28）：business_store 参数 → interpretation_store；删 ke_business_interp 注册。
    其他 ke_* 工具的 project_id 由 graph adapter（如 Neo4jGraphAdapter）实例化时绑定，
    本函数不直接经手。

    本函数构造 ke_* 核心工具（ke_search / ke_callees / ke_callers /
    ke_table_access / ke_impact）+ todo_write 元工具（设计 §3.3，无后端依赖始终注册）+ 2 个可选
    ke_* 工具（ke_read_entity / ke_method_interp，分别依赖 code_store / method_interp_store，
    None 时优雅缺省）+ 4 个文件类工具（ke_grep / ke_glob / ke_read_file / ke_ls，始终注册）。

    :param graph: 已绑 project_id 的 GraphProto adapter（如 Neo4jGraphAdapter(..., project_id=...)）
    :param interpretation_store: TopologicalInterpretation store 的 adapter（实现 InterpretationStoreProto）
    :param project_id: 当前请求工程 ID — 透传给 ke_search 闭包
    :param code_store: 可选 CodeEntity store
    :param method_interp_store: 可选 MethodInterpretation store
    :param repo_local_path: 项目源码本地绝对路径（DB projects.repo_local_path）。
        用于 4 个文件类工具（ke_grep / ke_glob / ke_read_file / ke_ls）；
        None 时 4 个工具仍注册但会返 "source path not configured" error。
    """
    registry = ToolRegistry()
    # ke_search 闭包绑 project_id（与 Neo4jGraphAdapter 设计对齐）
    registry.register(build_ke_search_tool(interpretation_store, project_id))
    registry.register(build_ke_callees_tool(graph))
    registry.register(build_ke_callers_tool(graph))
    registry.register(build_ke_table_access_tool(graph))
    registry.register(build_ke_impact_tool(graph))
    # 渲染类工具：调用图（图数据走前端内联渲染，summary 回灌 LLM）。设计 [[业务问答-agent化输出改造-设计]] §5
    # summary_lookup 暂留 None（label 回退方法短名）；后续可接 interpretation_store 取 2b 中文解读
    registry.register(build_render_call_graph_tool(graph))
    # meta 工具：todo_write 无后端依赖，始终注册（设计 §3.3）
    registry.register(build_todo_write_tool())
    if code_store is not None:
        registry.register(build_ke_read_entity_tool(code_store))
    if method_interp_store is not None:
        registry.register(build_ke_method_interp_tool(method_interp_store))
    # 4 个文件类工具：闭包绑定 repo_local_path（None 时 handler 自己返 error）
    # 设计：[[代码源文件查询工具-设计]] §3
    registry.register(build_ke_grep_tool(repo_local_path))
    registry.register(build_ke_glob_tool(repo_local_path))
    registry.register(build_ke_read_file_tool(repo_local_path))
    registry.register(build_ke_ls_tool(repo_local_path))
    return registry
