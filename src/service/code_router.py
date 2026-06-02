# src/service/code_router.py
"""代码片段查看端点：按 entity_id 返回源码片段 + 带调用点位置的 callees + callers。

支撑前端 IDE 式"点击实体看片段、片段内跳转"（设计 [[代码片段查看器-设计]]）。
独立 router：与 QA 聊天分离，是纯代码源访问，不依赖 Weaviate/LLM 基础设施。

依赖安装：无额外依赖，全部来自项目已有包。
"""
from __future__ import annotations  # PEP 563：类型注解可前向引用（避免循环导入/旧版本兼容）

import os  # 标准库 os：取文件后缀判语言（os.path.splitext）

# FastAPI 路由所需组件
# APIRouter：子路由器，通过 app.include_router 挂到主 app（解耦路由定义与 app 实例）
# Depends：依赖注入工厂，FastAPI 自动解析并调用（常用于 DB session / 鉴权）
# HTTPException：向客户端返回带状态码的 JSON 错误响应
# Query：声明 URL 查询参数（?entity_id=...），支持校验规则（min_length 等）
from fastapi import APIRouter, Depends, HTTPException, Query

# AsyncSession：SQLAlchemy 异步数据库会话（仅用于类型注解）
from sqlalchemy.ext.asyncio import AsyncSession

# 数据库 session 注入 dependency（测试可 override）
from src.service.db import get_db

# Project ORM 模型（projects 表），按主键取工程 + 读 repo_local_path
from src.service.db_models_homepage import Project as ProjectModel

# require_project_role：权限 dependency 工厂，校验 user 在 project 的 role ≥ min_role
# 传入 "reporter" 表示最低只读权限（与 /qa 端点同级门槛）
from src.service.permission_deps import require_project_role

# resolve_graph_adapter：按 repo_local_path 返回 CodeGraphGraphAdapter 或 NullGraphAdapter
# NullGraphAdapter：repo 未配 / 索引未建时的降级适配器（resolve_first 返 None）
from src.integrations.codegraph.graph_factory import resolve_graph_adapter

# read_snippet：按行号从源码文件读片段（1-indexed 闭区间，读不到返 ""）
from src.integrations.codegraph.source_reader import read_snippet

# resolve_safe_path：路径沙箱，防止 file_path 越界（../.. 逃逸、绝对路径），返回 Path 或 raise ValueError
from src.service.qa_engine.tools._path_sandbox import resolve_safe_path

# 文件后缀 → 前端 Monaco 编辑器 / 语法高亮用的语言标识符（小写匹配）
# 未收录的后缀统一用 "plaintext"（前端降级无高亮，不报错）
_LANG_BY_SUFFIX: dict[str, str] = {
    ".java": "java",           # Java 源文件
    ".xml": "xml",             # XML 配置（MyBatis mapper、Spring bean 等）
    ".py": "python",           # Python 源文件
    ".yml": "yaml",            # YAML 配置（Spring Boot application.yml 等）
    ".yaml": "yaml",           # YAML 的另一扩展名，与 .yml 等价
    ".sql": "sql",             # SQL 脚本
    ".js": "javascript",       # JavaScript 源文件
    ".ts": "typescript",       # TypeScript 源文件
    ".json": "json",           # JSON 数据/配置
    ".kt": "kotlin",           # Kotlin 源文件
    ".go": "go",               # Go 源文件
    ".properties": "ini",      # Java .properties（key=value 格式，Monaco 用 ini 高亮）
}


def _lang_from_path(file_path: str) -> str:
    """按文件后缀返回语言名（小写匹配）；未知后缀返回 'plaintext'。

    Args:
        file_path: 文件路径（相对或绝对均可），如 "mall-portal/src/Ctrl.java"
    Returns:
        Monaco/前端语法高亮用的语言字符串，如 "java"、"python"；未知后缀返回 "plaintext"
    """
    # os.path.splitext('a/B.java') → ('a/B', '.java')；取索引 [1] 得后缀
    # .lower()：统一转小写，避免 .Java / .JAVA 未命中映射表
    ext = os.path.splitext(file_path)[1].lower()

    # dict.get(key, default)：键不存在时返回 default，不抛 KeyError
    return _LANG_BY_SUFFIX.get(ext, "plaintext")


def build_snippet_response(
    graph_adapter,            # 图适配器（CodeGraphGraphAdapter 或 NullGraphAdapter 均可）
    repo_local_path: str,     # 工程源码根目录绝对路径（如 '/opt/repos/mall-swarm'）
    entity_id: str,           # 实体持久 key（qualified_name#params）
) -> dict | None:
    """组装 entity_id → 代码片段 JSON（纯逻辑，无 HTTP/鉴权，便于单测）。

    返回 None 表示"该实体无可用源码"（entity 未解析 / file_path 越界）
    → 调用方（路由层）据此返回 HTTP 404。

    Args:
        graph_adapter: 有 resolve_first / successors_with_locations / callers 方法的适配器对象
        repo_local_path: 工程源码根目录绝对路径
        entity_id: 实体持久 key（如 "OmsPortalOrderController::confirmReceiveOrder#(Long)"）
    Returns:
        spec §3 的 dict；**即使源码文件读不到，也返回 dict 且 code=""**（让前端显示"源码不可用"+元数据，而非 404）。
        返回 None 表示无可用源码：entity 未解析出（resolve_first→None），或 file_path 越界/
        repo_local_path 为空（resolve_safe_path 抛 ValueError）。
        注：repo_local_path 为空时此处返 None，但路由层应在调用本函数前先判 repo_local_path 是否配置、
        返更贴切的状态码，避免与"实体未找到"混淆。

    Examples:
        >>> out = build_snippet_response(adapter, "/repos/mall-swarm", "Ctrl::method#(Long)")
        >>> out["language"]  # "java"
        >>> out["code"]      # 源码片段字符串
    """
    # ── 1. entity_id → CgNode ────────────────────────────────────────────────
    # resolve_first：从 CodeGraph 按 entity_id 查第一个匹配节点，不存在返回 None
    node = graph_adapter.resolve_first(entity_id)
    # 节点不存在 → 调用方映射 404（实体未入图、entity_id 拼写错误等均走此分支）
    if node is None:
        return None

    # ── 2. 路径沙箱校验 ──────────────────────────────────────────────────────
    # node.file_path 来自 .codegraph.db，通常可信，但仍需防御：
    #   - 绝对路径（如 /etc/passwd）
    #   - 相对路径越界（如 ../../etc/passwd）
    # resolve_safe_path 抛 ValueError 时说明路径不合法，返回 None 让调用方 404
    try:
        # resolve_safe_path(repo_root, relative_path) → Path（已解析绝对路径）
        # 这里仅用于校验，不需要返回值
        resolve_safe_path(repo_local_path, node.file_path)
    except ValueError:
        # ValueError：路径越界或为绝对路径；静默返回 None，不暴露路径信息
        return None

    # ── 3. 读源码片段 ─────────────────────────────────────────────────────────
    # read_snippet(repo_root, file_path, start_line, end_line)
    #   → str，1-indexed 闭区间；文件不存在/行号非法返回 ""
    #   → 末尾不带换行（函数内部已处理），前端直接渲染
    code = read_snippet(repo_local_path, node.file_path, node.start_line, node.end_line)

    # ── 4. 组装 spec §3 响应 dict ────────────────────────────────────────────
    # 所有字段含义见设计文档 §3（代码片段查看器-设计.md §3 响应结构）
    return {
        "entity_id": entity_id,                          # 请求方传入的原始 key（前端用于路由/跳转）
        "qualified_name": node.qualified_name,           # 人类可读全限定名，如 "Ctrl::method"
        "kind": node.kind,                               # 实体类型（"method"/"class"/"interface"）
        "file_path": node.file_path,                     # 相对于 repo_local_path 的源文件路径
        "language": _lang_from_path(node.file_path),     # 语言标识符（前端语法高亮）
        "start_line": node.start_line,                   # 1-indexed 起始行（含）
        "end_line": node.end_line,                       # 1-indexed 结束行（含）
        "code": code,                                    # 源码片段字符串（空串=读不到）
        # callees：当前实体调用的下游实体列表，每项携带 line/col 便于前端点击跳转
        "callees": graph_adapter.successors_with_locations(entity_id),
        # callers：调用当前实体的上游实体列表（反向导航，用于"谁调用了我"）
        "callers": graph_adapter.callers(entity_id),
    }


# ── FastAPI Router（HTTP 层）────────────────────────────────────────────────────

# APIRouter 是 FastAPI 的子路由器，与 qa_router.py 用同款 prefix 约定。
# prefix="/projects/{project_id}"：所有路由都以这个路径前缀开头。
# tags=["code"]：OpenAPI 文档里把这些路由归入 "code" 分组（前端 Swagger 界面可见）。
# 注意：不挂 require_infra_healthy —— 本端点只读 CodeGraph + 源码文件，
#       不依赖 Weaviate/LLM，即使 infra 不健康时也应能正常提供代码查看功能。
router = APIRouter(prefix="/projects/{project_id}", tags=["code"])


@router.get(
    "/code-snippet",
    # dependencies 列表里的 Depends 会在路由函数执行前自动调用；
    # require_project_role("reporter") 返回一个 async checker 闭包：
    #   - 校验 project 存在（不存在 → 404）
    #   - 校验当前用户 role ≥ "reporter"（不满足 → 403）
    # 与 /qa/explain 端点使用相同的最低门槛（reporter = 只读访问权限）
    dependencies=[Depends(require_project_role("reporter"))],
)
async def get_code_snippet(
    project_id: str,  # 由 URL path 的 {project_id} 自动注入（与 prefix 中的占位符对应）
    # Query(...)：声明必填 URL 查询参数；min_length=1 防止传空串；description 用于 OpenAPI 文档
    entity_id: str = Query(..., min_length=1, description="实体持久 key，如 OmsXxx::m#(Long)"),
    # Depends(get_db)：FastAPI 自动调用 get_db 生成器，yield 出 AsyncSession 注入这里
    db: AsyncSession = Depends(get_db),
) -> dict:
    """按 entity_id 返回代码片段 + callees（带调用点 line/col）+ callers。

    404 三态：
      1. 工程不存在（由 require_project_role dependency 返回，路由函数体不执行）
      2. 工程未配置源码路径（repo_local_path is None）
      3. 该实体无可用源码（entity 未入图 / file_path 越界）

    403：用户无 reporter+ 权限（由 require_project_role dependency 返回）

    设计约束（见 [[代码片段查看器-设计]] §4.3）：
      - 不挂 require_infra_healthy：infra 不健康也能看代码
      - 纯 CodeGraph + 本地文件读取，无网络依赖
    """
    # ── 1. 取工程记录 + 校验 repo_local_path ──────────────────────────────────
    # db.get(Model, primary_key)：按主键查询（等价于 SELECT * FROM projects WHERE id = project_id）
    # 注意：require_project_role dependency 已经校验了工程存在性（不存在→404）；
    # 但 dependency 里使用的是独立 DB session，路由函数里需要重新查一次以确保数据一致性。
    p = await db.get(ProjectModel, project_id)
    if p is None:
        # 防御性兜底：正常情况下 require_project_role dependency 已先返 404（此处不可达）；
        # 仅当日后有人移除该 dependency 时，这里避免后续 p.repo_local_path 触发 AttributeError
        raise HTTPException(status_code=404, detail="工程不存在")

    # repo_local_path 为 None 或空串 → 工程未配置本地源码路径
    # 返回 404 而不是 500：这是"该工程尚未配置源码"的正常状态，非系统错误
    if not p.repo_local_path:
        raise HTTPException(status_code=404, detail="该工程未配置源码路径")

    # ── 2. 解析图适配器（CodeGraph 或 NullGraphAdapter）──────────────────────
    # resolve_graph_adapter：
    #   - .codegraph/codegraph.db 存在 → CodeGraphGraphAdapter（真实图查询）
    #   - 索引未建 / 路径不存在 → NullGraphAdapter（resolve_first 返 None → 下面 404）
    # 这样在索引尚未构建的环境里，端点也不会 500，只会 404 说明"实体无源码"
    graph_adapter = resolve_graph_adapter(p.repo_local_path)

    # ── 3. 调纯逻辑 helper 组装响应 ─────────────────────────────────────────
    # build_snippet_response：无 HTTP/鉴权，纯函数（已有独立单测覆盖）
    # 返回 None 表示：entity 未解析出 / file_path 越界
    result = build_snippet_response(graph_adapter, p.repo_local_path, entity_id)

    if result is None:
        # 404 detail 说明"实体无源码"（区分于"工程不存在"和"路径未配置"）
        raise HTTPException(status_code=404, detail="未找到该实体的源码")

    # 返回 dict；FastAPI 自动序列化为 JSON（Content-Type: application/json）
    return result
