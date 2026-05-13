"""Audit Log Query API（v2.0 Task 11）。

提供两个只读端点供管理员和 Group Owner 查询审计日志：

  GET /admin/audit-logs
      Instance Admin 全局查询，支持多维过滤 + 分页。

  GET /groups/{gid}/audit-logs
      Group Owner 查看本组及子孙 group 的相关日志（group / project / member 类型）。

设计原则：
  - 查询只读，不写库
  - admin 端依赖 require_admin（已在 admin_router 定义）
  - group 端依赖 require_group_role("owner")
  - 分页用 OFFSET/LIMIT；总数用独立 COUNT(*) 查询
  - metadata_json 字段 JSON 反序列化为 dict 后返回
"""
from __future__ import annotations  # 延迟注解求值（Python 3.9 兼容）

import json                  # 标准库：JSON 字符串 → dict 反序列化
from datetime import datetime  # 标准库：日期时间类型，用于查询参数类型注解
from typing import Optional  # Optional[X] = Union[X, None]，字段可为 None

# FastAPI 核心组件
from fastapi import APIRouter, Depends, Query  # Query：定义查询参数（含验证 + 描述）

# Pydantic：请求/响应 Schema 基类
from pydantic import BaseModel, Field  # BaseModel：Schema 基类；Field：字段级约束

# SQLAlchemy 2.0 查询构建
from sqlalchemy import and_, func, or_, select  # 查询构建工具
from sqlalchemy.ext.asyncio import AsyncSession  # 异步 session 类型注解

# 项目内模块
from src.service.admin_router import require_admin  # Instance Admin 权限守卫
from src.service.auth_dependencies import get_current_user  # JWT → 当前用户
from src.service.auth_models import User                    # 用户 ORM 模型
from src.service.db import get_db                          # FastAPI DB session dependency
from src.service.db_models_groups import AuditLog          # 审计日志 ORM 模型
from src.service.db_models_homepage import Project          # Project ORM 模型（查 group 下项目）
from src.service.permission_deps import require_group_role  # Group 权限 dependency 工厂


# ─── Router 定义 ──────────────────────────────────────────────────────────────

# audit_router 不设 prefix：两条路由的 prefix 不同（/admin 和 /groups），
# 分别注册即可；在 api.py 里 include_router 时直接加。
router = APIRouter(tags=["audit"])


# ─── Pydantic 响应 Schema ─────────────────────────────────────────────────────

class AuditLogEntry(BaseModel):
    """单条审计日志的响应体。

    字段与 audit_logs 表列一一对应，metadata_json 已反序列化为 dict。

    Attributes:
        id:             审计日志自增整数 ID。
        actor_user_id:  操作人用户 ID（用户注销后为 None）。
        actor_username: 操作人用户名（JOIN users 得到；用户注销后为 None）。
        action:         操作动作字符串，如 'project.create'。
        resource_type:  资源类型，如 'project' / 'group'。
        resource_id:    资源 ID 字符串。
        metadata:       操作上下文 dict（原 metadata_json 反序列化）。
        ip_address:     客户端 IP（可为 None）。
        created_at:     操作时间，ISO8601 字符串。
    """
    id: int                               # 审计日志整数主键
    actor_user_id: Optional[int]          # 操作人 ID（可为 None，用户注销后）
    actor_username: Optional[str]         # 操作人用户名（JOIN users 得到）
    action: str                           # 操作动作字符串
    resource_type: str                    # 资源类型
    resource_id: str                      # 资源 ID
    metadata: dict                        # 操作上下文（已从 JSON 反序列化）
    ip_address: Optional[str]             # 客户端 IP
    created_at: str                       # 操作时间（ISO8601 字符串）


class AuditLogResponse(BaseModel):
    """审计日志分页查询响应体。

    Attributes:
        entries: 当前页的日志条目列表。
        total:   满足过滤条件的日志总数（用于前端分页控件）。
        page:    当前页码（从 1 开始）。
        limit:   每页条目数上限。
    """
    entries: list[AuditLogEntry]  # 当前页日志列表
    total: int                    # 满足条件的总数（分页用）
    page: int                     # 当前页码
    limit: int                    # 本次请求的 limit 值


# ─── 工具函数 ─────────────────────────────────────────────────────────────────

def _row_to_entry(row) -> AuditLogEntry:
    """把 SQLAlchemy SELECT 返回的 Row（含 JOIN 列）转换为 AuditLogEntry。

    ROW 列：id, actor_user_id, action, resource_type, resource_id,
            metadata_json, ip_address, created_at, actor_username

    Args:
        row: SQLAlchemy Row 对象（命名元组风格，可用 row.列名 访问）。

    Returns:
        AuditLogEntry：Pydantic Schema 实例。
    """
    # json.loads：把 JSON 字符串反序列化为 Python dict
    # 若 metadata_json 是 None 或空字符串，改为 "{}" 后再解析，保证返回 dict 类型
    raw_meta = row.metadata_json or "{}"
    try:
        meta: dict = json.loads(raw_meta)  # str → dict
    except (json.JSONDecodeError, TypeError):
        # JSON 解析失败（数据损坏）：返回空 dict，不让查询崩溃
        meta = {}

    return AuditLogEntry(
        id=row.id,
        actor_user_id=row.actor_user_id,
        # actor_username：JOIN users 字段，可能是 None（用户注销时 FK SET NULL）
        actor_username=getattr(row, "actor_username", None),
        action=row.action,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        metadata=meta,
        ip_address=row.ip_address,
        # .isoformat()：datetime → ISO8601 字符串（如 '2026-05-12T08:00:00'）
        created_at=row.created_at.isoformat() if row.created_at else "",
    )


def _validate_page_limit(page: int, limit: int) -> tuple[int, int]:
    """校验并规范化分页参数。

    Args:
        page:  页码，必须 >= 1。
        limit: 每页条目数，范围 [1, 200]。

    Returns:
        tuple[int, int]：规范化后的 (page, limit)。
        若参数越界，自动 clamp 到合法范围（不抛错）。
    """
    # max(1, page)：确保 page 至少为 1（防负数或 0）
    page = max(1, page)
    # min(max(1, limit), 200)：确保 limit 在 [1, 200] 范围内
    limit = min(max(1, limit), 200)
    return page, limit


async def _expand_group_and_descendants(group_id: str, db: AsyncSession) -> set[str]:
    """从指定 group 出发，BFS 向下展开所有子孙 group id（含自身）。

    用于 /groups/{gid}/audit-logs：查询本组及其子孙 group 下的所有日志。

    算法与 permission_deps._expand_user_groups 类似，但这里不需要"用户成员关系"过滤，
    直接展开所有子孙。

    Args:
        group_id: 起始 group 的字符串 ID。
        db:       异步数据库 session。

    Returns:
        set[str]：所有相关 group id 的集合（含 group_id 本身 + 所有子孙）。
    """
    # 导入 Group ORM 模型（permission_deps 里已经有类似逻辑，这里直接查）
    from src.service.db_models_groups import Group

    # 初始化：从目标 group 本身开始
    all_ids: set[str] = {group_id}   # 所有已发现的 group id（含自身）
    frontier: set[str] = {group_id}  # BFS 当前层（初始只有根）

    # BFS：最多向下展开 3 层（与 spec MAX_GROUP_DEPTH 一致，防止数据异常时的无限循环）
    # range(3)：循环 3 次，分别对应深度 1 / 2 / 3 的子 group
    for _ in range(3):
        if not frontier:
            break  # 没有待展开的 group，提前退出

        # 查所有 parent_group_id 在 frontier 集合里的子 group id
        # Group.parent_group_id.in_(frontier)：生成 WHERE parent_group_id IN (...) SQL
        children: set[str] = set(
            (await db.scalars(
                select(Group.id).filter(Group.parent_group_id.in_(frontier))
            )).all()
        )

        # new = children - all_ids：只取"尚未发现的新子 group"（差集，防重复）
        new: set[str] = children - all_ids
        if not new:
            break  # 没有新的子 group，BFS 结束

        all_ids.update(new)  # 把新子 group 加入总集合
        frontier = new       # 下一层 BFS 从新发现的子 group 开始

    return all_ids  # 返回所有相关 group id（含自身 + 所有子孙）


# ─── 路由 1：GET /admin/audit-logs（Instance Admin 全局查询）────────────────────

@router.get(
    "/admin/audit-logs",
    response_model=AuditLogResponse,  # 响应自动序列化为 AuditLogResponse JSON
)
async def list_admin_audit_logs(
    # ── 过滤参数（Query 参数，URL 里用 ?key=value 传入）────────────────────────
    # Query(None, description=...)：可选参数，默认 None；description 显示在 OpenAPI 文档里
    actor: Optional[str] = Query(None, description="模糊匹配操作人用户名（LIKE '%actor%'）"),
    actor_user_id: Optional[int] = Query(None, description="精确匹配操作人 user ID"),
    resource_type: Optional[str] = Query(None, description="资源类型：project / group / ..."),
    resource_id: Optional[str] = Query(None, description="精确匹配资源 ID"),
    action_prefix: Optional[str] = Query(None, description="动作前缀（如 'project.' 匹配所有 project 操作）"),
    from_time: Optional[datetime] = Query(None, description="时间范围起点（ISO8601，含）"),
    to_time: Optional[datetime] = Query(None, description="时间范围终点（ISO8601，含）"),
    page: int = Query(1, ge=1, description="页码，从 1 开始"),  # ge=1：Query 层的参数校验，<1 返回 422
    limit: int = Query(50, ge=1, le=200, description="每页条目数，[1, 200]"),
    # ── FastAPI 依赖注入 ────────────────────────────────────────────────────────
    _admin: User = Depends(require_admin),  # 下划线：只做权限守卫，不在函数体里用
    db: AsyncSession = Depends(get_db),
) -> AuditLogResponse:
    """查询全局审计日志（Instance Admin only）。

    支持按操作人、资源类型、动作前缀、时间范围过滤，结果按 created_at DESC 排序。
    同时返回满足条件的总数，供前端分页控件使用。

    Args:
        actor:          操作人用户名模糊匹配（LIKE）。
        actor_user_id:  操作人 user ID 精确匹配。
        resource_type:  资源类型精确匹配。
        resource_id:    资源 ID 精确匹配。
        action_prefix:  动作前缀（LIKE 'prefix%'）。
        from_time:      时间范围起点（含）。
        to_time:        时间范围终点（含）。
        page:           页码（从 1 开始）。
        limit:          每页条目数（上限 200）。
        _admin:         require_admin dependency 注入的 admin 用户（仅用于权限守卫）。
        db:             异步数据库 session。

    Returns:
        AuditLogResponse：当前页日志条目列表 + 总数 + 分页信息。
    """
    # ── 规范化分页参数 ────────────────────────────────────────────────────────
    page, limit = _validate_page_limit(page, limit)

    # ── 构造过滤条件列表 ──────────────────────────────────────────────────────
    # conditions：所有 WHERE 子句条件的列表，最后用 and_(*conditions) 合并
    conditions = []

    # 1. 按操作人用户名模糊匹配（需要 JOIN users 表）
    if actor is not None:
        # User.username.ilike(f"%{actor}%")：大小写不敏感的 LIKE 匹配
        # ilike：SQLAlchemy 的 ILIKE 操作符（底层 SQLite 用 LIKE，MySQL 默认不区分大小写）
        conditions.append(User.username.ilike(f"%{actor}%"))

    # 2. 按操作人 user ID 精确匹配
    if actor_user_id is not None:
        conditions.append(AuditLog.actor_user_id == actor_user_id)

    # 3. 按资源类型精确匹配
    if resource_type is not None:
        conditions.append(AuditLog.resource_type == resource_type)

    # 4. 按资源 ID 精确匹配
    if resource_id is not None:
        conditions.append(AuditLog.resource_id == resource_id)

    # 5. 按动作前缀过滤（LIKE 'prefix%'）
    if action_prefix is not None:
        # AuditLog.action.startswith(...)：生成 LIKE 'prefix%' SQL
        # startswith 等价于 .like(f"{action_prefix}%")
        conditions.append(AuditLog.action.startswith(action_prefix))

    # 6. 按时间范围过滤（from_time <= created_at <= to_time）
    if from_time is not None:
        conditions.append(AuditLog.created_at >= from_time)  # >= 含起点
    if to_time is not None:
        conditions.append(AuditLog.created_at <= to_time)    # <= 含终点

    # ── 构造 JOIN 语句：LEFT JOIN users 获取 actor_username ────────────────────
    # outerjoin：LEFT OUTER JOIN，保证 actor_user_id = NULL 的日志也能被查到
    # AuditLog.actor_user_id == User.id：JOIN 条件
    # User.username.label("actor_username")：给 JOIN 列起别名，Row 里用 row.actor_username 访问
    join_clause = (
        select(
            AuditLog.id,
            AuditLog.actor_user_id,
            AuditLog.action,
            AuditLog.resource_type,
            AuditLog.resource_id,
            AuditLog.metadata_json,
            AuditLog.ip_address,
            AuditLog.created_at,
            User.username.label("actor_username"),  # JOIN 得到的 username 列，别名 actor_username
        )
        .outerjoin(User, AuditLog.actor_user_id == User.id)  # LEFT JOIN users
    )

    # ── 应用所有过滤条件 ───────────────────────────────────────────────────────
    if conditions:
        # and_(*conditions)：把 conditions 列表里所有条件用 AND 连接
        # *conditions：Python 的解包操作（unpack），把列表展开为位置参数
        join_clause = join_clause.where(and_(*conditions))

    # ── COUNT 查询（单独一次查询，获取满足条件的总数）───────────────────────────
    # 把 join_clause 包成子查询（subquery），再对它做 SELECT COUNT(*)
    # 这是"先构造过滤再统计总数"的标准做法，避免两次写同样的 WHERE 子句
    count_subq = join_clause.subquery()  # 转为子查询对象
    total: int = await db.scalar(
        select(func.count()).select_from(count_subq)  # SELECT COUNT(*) FROM (subquery)
    ) or 0  # db.scalar 可能返回 None（空表），or 0 保证是 int

    # ── 分页查询（加 ORDER BY + OFFSET + LIMIT）────────────────────────────────
    # offset：跳过多少条（(page-1) * limit 是标准分页偏移计算）
    offset = (page - 1) * limit

    data_rows = (
        await db.execute(
            join_clause
            .order_by(AuditLog.created_at.desc())  # 按创建时间倒序
            .offset(offset)                         # 跳过前 N 条
            .limit(limit)                           # 取 limit 条
        )
    ).fetchall()  # fetchall()：把所有行取回 Python（Row 对象列表）

    # ── 组装响应 ──────────────────────────────────────────────────────────────
    # _row_to_entry：把 Row 对象转换为 AuditLogEntry Pydantic Schema
    # 列表推导式：[_row_to_entry(r) for r in data_rows] 对每行调用转换函数
    entries = [_row_to_entry(r) for r in data_rows]

    return AuditLogResponse(
        entries=entries,
        total=total,
        page=page,
        limit=limit,
    )


# ─── 路由 2：GET /groups/{gid}/audit-logs（Group Owner 查看本组日志）────────────

@router.get(
    "/groups/{group_id}/audit-logs",
    response_model=AuditLogResponse,
)
async def list_group_audit_logs(
    group_id: str,  # FastAPI 从 URL 路径 {group_id} 自动提取
    # ── 分页参数 ────────────────────────────────────────────────────────────────
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    limit: int = Query(50, ge=1, le=200, description="每页条目数，[1, 200]"),
    # ── FastAPI 依赖注入 ────────────────────────────────────────────────────────
    # require_group_role("owner")：Group Owner（或 Instance Admin）才能访问
    # 注意：这里必须把 dependency 结果注入到参数里（而不是放 dependencies=[] 里），
    # 因为 group_id 同时出现在 require_group_role 的 checker 里（FastAPI 会自动匹配同名路径参数）
    _role: str = Depends(require_group_role("owner")),
    db: AsyncSession = Depends(get_db),
) -> AuditLogResponse:
    """查询 group 相关审计日志（Group Owner only）。

    范围：
      - resource_type='group'  AND resource_id IN (gid + 子孙 group ids)
      - OR resource_type='project' AND resource_id IN (这些 group 下的所有 project id)
      - OR resource_type='group_member' AND resource_id IN (gid + 子孙)
      - OR resource_type='project_member' AND resource_id IN (相关项目 id)

    按 created_at DESC 排序 + 分页。

    Args:
        group_id:  路径参数，目标 group 的业务 ID。
        page:      页码（从 1 开始）。
        limit:     每页条目数（上限 200）。
        _role:     require_group_role("owner") dependency 注入的角色字符串（仅权限守卫用）。
        db:        异步数据库 session。

    Returns:
        AuditLogResponse：当前页日志条目列表 + 总数 + 分页信息。
    """
    # ── 规范化分页参数 ────────────────────────────────────────────────────────
    page, limit = _validate_page_limit(page, limit)

    # ── 步骤 1：BFS 展开所有子孙 group id（含自身）──────────────────────────────
    # _expand_group_and_descendants：向下 BFS，最多 3 层
    group_ids: set[str] = await _expand_group_and_descendants(group_id, db)

    # ── 步骤 2：查所有相关 project id（这些 group 下的所有工程）──────────────────
    # Project.group_id.in_(group_ids)：WHERE group_id IN (...) SQL
    project_ids: list[str] = list(
        (await db.scalars(
            select(Project.id).filter(Project.group_id.in_(group_ids))
        )).all()
    )

    # ── 步骤 3：构造 OR 条件组合（group / project / group_member / project_member）──
    # 把 group_ids 和 project_ids 转为列表（SQLAlchemy .in_() 接受可迭代对象）
    gid_list = list(group_ids)

    # or_(...)：SQL OR，把多个条件用 OR 连接（任一条件满足即返回）
    # 每个条件：resource_type = 'xxx' AND resource_id IN (...)
    # and_(...)：SQL AND，两个条件同时满足
    scope_conditions = []

    # 条件 1：group 操作（本组及子孙 group 本身）
    scope_conditions.append(
        and_(
            AuditLog.resource_type == "group",
            AuditLog.resource_id.in_(gid_list),  # 在 group_ids 集合里
        )
    )

    # 条件 2：group 成员操作（resource_id 是 group_id）
    scope_conditions.append(
        and_(
            AuditLog.resource_type == "group_member",
            AuditLog.resource_id.in_(gid_list),
        )
    )

    # 条件 3：project 操作（project 的 resource_id 是 project id）
    # 只有当有关联工程时才加此条件（避免空列表导致 WHERE IN () 语法错误）
    if project_ids:
        scope_conditions.append(
            and_(
                AuditLog.resource_type == "project",
                AuditLog.resource_id.in_(project_ids),
            )
        )

    # 条件 4：project 成员操作（resource_id 是 project id）
    if project_ids:
        scope_conditions.append(
            and_(
                AuditLog.resource_type == "project_member",
                AuditLog.resource_id.in_(project_ids),
            )
        )

    # or_(*scope_conditions)：把所有条件用 OR 连接（任一满足即包含）
    scope_filter = or_(*scope_conditions)

    # ── 步骤 4：构造 LEFT JOIN + WHERE 查询 ────────────────────────────────────
    # 与 admin 端相同：LEFT JOIN users 获取 actor_username
    base_query = (
        select(
            AuditLog.id,
            AuditLog.actor_user_id,
            AuditLog.action,
            AuditLog.resource_type,
            AuditLog.resource_id,
            AuditLog.metadata_json,
            AuditLog.ip_address,
            AuditLog.created_at,
            User.username.label("actor_username"),
        )
        .outerjoin(User, AuditLog.actor_user_id == User.id)  # LEFT JOIN users
        .where(scope_filter)                                  # 限定范围
    )

    # ── 步骤 5：COUNT 总数 ────────────────────────────────────────────────────
    count_subq = base_query.subquery()  # 转子查询
    total: int = await db.scalar(
        select(func.count()).select_from(count_subq)
    ) or 0

    # ── 步骤 6：分页查询 ──────────────────────────────────────────────────────
    offset = (page - 1) * limit
    data_rows = (
        await db.execute(
            base_query
            .order_by(AuditLog.created_at.desc())  # 按创建时间倒序
            .offset(offset)
            .limit(limit)
        )
    ).fetchall()

    # ── 步骤 7：组装响应 ──────────────────────────────────────────────────────
    entries = [_row_to_entry(r) for r in data_rows]

    return AuditLogResponse(
        entries=entries,
        total=total,
        page=page,
        limit=limit,
    )
