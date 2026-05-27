"""Project Members CRUD 路由 (v2.0)。

接口列表（前缀 /projects/{project_id}）：
  - GET    /projects/{pid}/members           列出成员（reporter+）
                                             返回 {direct: [...], inherited: [...]}
  - POST   /projects/{pid}/members           加成员（owner）
  - PATCH  /projects/{pid}/members/{uid}     改成员 role（owner）
  - DELETE /projects/{pid}/members/{uid}     删成员（owner；保护最后一个 direct owner）

关键约束：
  1. 表：user_project_access（直接成员）+ GroupMember（继承成员）
  2. GET 返回结构：{direct: [...], inherited: [...]}
     - direct：来自 user_project_access 的直接成员
     - inherited：来自 project.group_id 向上祖先 group 的所有 GroupMember
  3. role 合法值：'reporter' / 'maintainer' / 'owner'
  4. Last-owner 保护：仅保护 direct owner（user_project_access 表），
     不把 inherited owner 算进去（inherited 成员无法在本项目层面单独删除）
  5. 审计动作：PROJECT_MEMBER_ADD / REMOVE / ROLE_CHANGE

设计文档：[[多工程隔离与权限-设计]] (Obsidian)
"""

# from __future__ import annotations：启用"延迟注解求值"
# 让 Python 3.9 以前版本也能写 list[str] / Optional[str] 等注解
from __future__ import annotations

from typing import Optional  # Optional[X] = Union[X, None]

# FastAPI 核心组件
from fastapi import APIRouter, Depends, HTTPException, Request, status

# Pydantic：数据验证 + 序列化
from pydantic import BaseModel, Field

# SQLAlchemy 2.0 查询构造器
from sqlalchemy import func, select

# AsyncSession：异步数据库会话类型
from sqlalchemy.ext.asyncio import AsyncSession

# 项目内模块
from src.service.audit.logger import log_audit   # 统一审计日志
from src.service.audit import actions             # 审计动作常量
from src.service.auth_dependencies import get_current_user  # JWT → User
from src.service.auth_models import User          # 用户 ORM 模型
from src.service.db import get_db                 # DB 依赖
from src.service.db_models_groups import Group, GroupMember   # Group 继承相关 ORM
from src.service.db_models_homepage import Project, UserProjectAccess  # 工程 + 直接成员 ORM
from src.service.permission_deps import (
    ROLE_RANK,               # role 等级字典：{'reporter': 1, 'maintainer': 2, 'owner': 3}
    require_project_role,    # dependency 工厂：检查 role ≥ min_role
)
from src.service.deps_infra import require_infra_healthy  # 设计 §3.3：基础设施不可用时返 503


# ─── Router 定义 ──────────────────────────────────────────────────────────────

# APIRouter：FastAPI 路由分组工具
# prefix="/projects"：所有路由路径前自动加上 "/projects"
router = APIRouter(
    prefix="/projects",
    tags=["project-members"],
    # 设计 §3.3：任一 critical 依赖挂 → 503 INFRA_UNHEALTHY
    dependencies=[Depends(require_infra_healthy)],
)


# ─── Pydantic 请求 / 响应 Schema ──────────────────────────────────────────────

class MemberAddRequest(BaseModel):
    """POST /projects/{pid}/members 请求体。

    Attributes:
        user_id: 要加入的用户整数 ID（必填）。
        role:    赋予的角色，必须是 reporter / maintainer / owner 之一。
    """
    user_id: int  # 目标用户的数据库整数 ID
    # Field(..., pattern=...)：Pydantic 正则校验
    # 只允许三个枚举字符串，其他值触发 422 Unprocessable Entity
    role: str = Field(
        ...,
        pattern=r"^(reporter|maintainer|owner)$",
    )


class MemberRoleUpdateRequest(BaseModel):
    """PATCH /projects/{pid}/members/{uid} 请求体。

    Attributes:
        role: 新 role，必须是三个合法值之一。
    """
    role: str = Field(
        ...,
        pattern=r"^(reporter|maintainer|owner)$",
    )


class DirectMemberResponse(BaseModel):
    """直接成员响应体（来自 user_project_access 表）。

    Attributes:
        user_id:    成员的用户整数 ID。
        project_id: 所属工程 ID（字符串）。
        role:       当前角色。
    """
    user_id: int
    project_id: str
    role: str


class InheritedMemberResponse(BaseModel):
    """继承成员响应体（来自祖先 group 的 GroupMember）。

    Attributes:
        user_id:                 成员用户 ID。
        username:                用户名（方便前端显示，避免再查一次）。
        role:                    从 group 继承来的 role（多组取最高）。
        inherited_from_group_id: 贡献此 role 的最近 group ID。
    """
    user_id: int
    username: str
    role: str
    inherited_from_group_id: str


class ProjectMembersResponse(BaseModel):
    """GET /projects/{pid}/members 响应体。

    结构：{direct: [...], inherited: [...]}

    Attributes:
        direct:    直接成员列表（来自 user_project_access 表）。
        inherited: 继承成员列表（来自祖先 group，不含已出现在 direct 里的用户）。
    """
    # list[...] 是 Python 3.9+ 写法；from __future__ import annotations 让低版本也支持
    direct: list[DirectMemberResponse]
    inherited: list[InheritedMemberResponse]


# ─── 内部工具函数 ─────────────────────────────────────────────────────────────

async def _count_direct_owners(project_id: str, db: AsyncSession) -> int:
    """统计 project 的直接 owner 数量（仅 user_project_access 表）。

    用于"最后一个 direct owner 保护"：
    - DELETE 前：如果 direct owner 数量 = 1 且要删的正是那个 owner → 422
    - PATCH 前：如果 direct owner 数量 = 1 且要降级的正是那个 owner → 422

    注意：inherited owner（通过 group 继承）不算在内，
    因为单个项目层面无法单独删除继承成员。

    Args:
        project_id: 目标工程的字符串 ID。
        db:         异步数据库 session。

    Returns:
        int：direct owner 数量（>= 0）。
    """
    # func.count(...)：生成 SQL COUNT(...) 聚合函数
    # filter_by(project_id=project_id, role="owner")：WHERE project_id=? AND role='owner'
    count = await db.scalar(
        select(func.count(UserProjectAccess.user_id)).filter_by(
            project_id=project_id,
            role="owner",
        )
    )
    # db.scalar 返回 None 时（空表），用 or 0 保证返回 int
    return count or 0


async def _list_inherited_members(
    project_id: str,
    db: AsyncSession,
) -> list[InheritedMemberResponse]:
    """计算 project 的继承成员列表。

    从 project.group_id 开始，向上遍历 parent_group_id（深度上限 3），
    找所有祖先 group 的 GroupMember。同一用户在多个 group 里时取 role 最高的。

    Args:
        project_id: 目标工程字符串 ID。
        db:         异步数据库 session。

    Returns:
        list[InheritedMemberResponse]：继承成员列表（去重后按 user_id 一条记录）。
    """
    # ── 1. 查 project 的 group_id ─────────────────────────────────────────
    # db.get(Model, pk)：按主键查询，比 select+filter 更简洁
    project = await db.get(Project, project_id)

    # project 不存在或没有关联 group → 无继承成员
    if not project or not project.group_id:
        return []

    # ── 2. 收集祖先 group ID 链（深度上限 3）────────────────────────────
    # ancestor_gids：包含 project 直接归属的 group 及其所有祖先的 ID 列表
    ancestor_gids: list[str] = []
    cur: Optional[str] = project.group_id  # 从 project 的直接 group 开始
    visited: set[str] = set()              # 防止循环引用
    depth: int = 0                          # 深度计数器（最多 3 层）

    # while 循环：向上遍历祖先 group
    while cur and cur not in visited and depth < 3:
        ancestor_gids.append(cur)   # 记录当前 group id
        visited.add(cur)            # 标记已访问，防循环

        # 查当前 group 的父 group id
        # db.scalar(select(Group.parent_group_id).filter_by(id=cur))：
        # SELECT parent_group_id FROM groups WHERE id = cur LIMIT 1
        parent = await db.scalar(
            select(Group.parent_group_id).filter_by(id=cur)
        )
        cur = parent  # 向上走一层（根 group 的 parent 是 None，循环自然结束）
        depth += 1    # 深度 +1

    # 如果没有找到任何祖先 group（project 的 group_id 不对应真实 group）
    if not ancestor_gids:
        return []

    # ── 3. 查所有祖先 group 的 GroupMember 记录（JOIN User 取 username）────
    # select(GroupMember, User)：同时选 GroupMember 和 User 两个模型
    # .join(User, User.id == GroupMember.user_id)：INNER JOIN users ON users.id = group_members.user_id
    # .filter(GroupMember.group_id.in_(ancestor_gids))：WHERE group_id IN (...)
    result = await db.execute(
        select(GroupMember, User)
        .join(User, User.id == GroupMember.user_id)
        .filter(GroupMember.group_id.in_(ancestor_gids))
    )

    # ── 4. 同一用户在多个 group 里 → 取 role 最高的记录 ──────────────────
    # by_user：dict，key=user_id，value=(GroupMember, User) 元组
    # 每次新遇到同一用户时，比较 ROLE_RANK，保留更高的
    by_user: dict[int, tuple[GroupMember, User]] = {}

    # result.all()：把查询结果的所有行一次取出（每行是 (GroupMember, User) 元组）
    for member, user in result.all():
        existing = by_user.get(user.id)
        if existing is None:
            # 第一次遇到这个用户，直接存入
            by_user[user.id] = (member, user)
        else:
            # 已有记录，比较 role 等级，保留更高的
            existing_member, _ = existing
            # ROLE_RANK[m.role]：字典取值，得到 role 的整数等级（reporter=1, maintainer=2, owner=3）
            if ROLE_RANK[member.role] > ROLE_RANK[existing_member.role]:
                by_user[user.id] = (member, user)

    # ── 5. 转换为 InheritedMemberResponse 列表 ────────────────────────────
    # 列表推导式：对 by_user 里每个 (member, user) 元组生成 InheritedMemberResponse
    return [
        InheritedMemberResponse(
            user_id=user.id,
            username=user.username,
            role=member.role,
            inherited_from_group_id=member.group_id,  # 贡献此 role 的 group
        )
        for member, user in by_user.values()
    ]


def _to_direct_response(acc: UserProjectAccess) -> DirectMemberResponse:
    """将 UserProjectAccess ORM 实例转换为 DirectMemberResponse。

    DRY 原则：统一 GET / POST / PATCH 的直接成员返回格式。

    Args:
        acc: UserProjectAccess ORM 实例。

    Returns:
        DirectMemberResponse：标准化直接成员响应体。
    """
    return DirectMemberResponse(
        user_id=acc.user_id,
        project_id=acc.project_id,
        role=acc.role,
    )


# ─── 路由：GET /projects/{project_id}/members（列成员）────────────────────────

@router.get(
    "/{project_id}/members",
    response_model=ProjectMembersResponse,   # 返回 {direct: [...], inherited: [...]}
    # require_project_role("reporter")：reporter+ 才能查看成员列表
    dependencies=[Depends(require_project_role("reporter"))],
)
async def list_project_members(
    project_id: str,                       # FastAPI 从 URL path 提取 {project_id}
    db: AsyncSession = Depends(get_db),    # 依赖注入：异步 DB session
) -> ProjectMembersResponse:
    """列出 project 的所有成员，分 direct 和 inherited 两组（需 reporter+ 权限）。

    direct：直接在 user_project_access 表里有记录的用户。
    inherited：通过 project.group_id 向上祖先 group 继承来的成员。

    同一用户如果既是 direct 也是 inherited，只出现在 direct 列表里（后端去重）。

    Args:
        project_id: 路径参数，目标工程 ID。
        db:         异步 DB session。

    Returns:
        ProjectMembersResponse：{direct: [...], inherited: [...]}

    Raises:
        HTTPException(404): 工程不存在（由 require_project_role dependency 处理）。
        HTTPException(403): 权限不足。
    """
    # ── 1. 查直接成员（user_project_access 表）────────────────────────────
    # db.scalars(select(Model).filter_by(...)).all()：
    # 查满足条件的所有 UserProjectAccess 记录，返回 ORM 对象列表
    direct_records = (await db.scalars(
        select(UserProjectAccess).filter_by(project_id=project_id)
    )).all()

    # 转换为响应体格式
    direct_list = [_to_direct_response(acc) for acc in direct_records]

    # ── 2. 查继承成员（祖先 group 的 GroupMember）────────────────────────
    inherited_all = await _list_inherited_members(project_id, db)

    # ── 3. 去重：已在 direct 里的用户不再出现在 inherited 里 ──────────────
    # set comprehension：{m.user_id for m in direct_records} → 直接成员的 user_id 集合
    # 集合的 in 操作是 O(1)，比 list 的 O(n) 更快
    direct_uids: set[int] = {acc.user_id for acc in direct_records}

    # 列表推导式：过滤掉 user_id 已在 direct 里的 inherited 成员
    inherited_list = [
        m for m in inherited_all if m.user_id not in direct_uids
    ]

    return ProjectMembersResponse(
        direct=direct_list,
        inherited=inherited_list,
    )


# ─── 路由：POST /projects/{project_id}/members（加成员）──────────────────────

@router.post(
    "/{project_id}/members",
    response_model=DirectMemberResponse,
    status_code=201,   # 创建成功返回 201 Created
    # require_project_role("owner")：只有 owner 才能加成员
    dependencies=[Depends(require_project_role("owner"))],
)
async def add_project_member(
    project_id: str,
    body: MemberAddRequest,                          # 请求体：user_id + role
    request: Request,                                # 原始请求，用于提取客户端 IP
    user: User = Depends(get_current_user),          # 当前登录用户（操作者）
    db: AsyncSession = Depends(get_db),
) -> DirectMemberResponse:
    """往 project 加一个直接成员（需 owner 权限）。

    规则：
      - target user 必须存在
      - 不能重复加（已存在直接成员 → 409）
      - role 必须是合法值（Pydantic 已校验，非法 → 422）
      - 写审计日志 PROJECT_MEMBER_ADD

    Args:
        project_id: 路径参数，目标工程 ID。
        body:       请求体（user_id + role）。
        request:    FastAPI Request，用于提取客户端 IP。
        user:       当前登录用户（操作者）。
        db:         异步 DB session。

    Returns:
        DirectMemberResponse：新加成员的信息。

    Raises:
        HTTPException(404): 目标用户不存在。
        HTTPException(409): 用户已经是直接成员。
    """
    # ── 目标用户必须存在 ─────────────────────────────────────────────────────
    target_user = await db.get(User, body.user_id)
    if not target_user:
        raise HTTPException(
            status_code=404,
            detail="用户不存在",
        )

    # ── 不能重复加（幂等保护）────────────────────────────────────────────────
    # UserProjectAccess 复合主键 (user_id, project_id)，用 db.get 传 tuple
    existing = await db.get(UserProjectAccess, (body.user_id, project_id))
    if existing:
        raise HTTPException(
            status_code=409,
            detail="该用户已经是成员",
        )

    # ── 创建 UserProjectAccess ORM 实例 ──────────────────────────────────────
    # UserProjectAccess：user_project_access 表的 ORM 模型，复合主键 (user_id, project_id)
    access = UserProjectAccess(
        user_id=body.user_id,      # 被加入的用户 ID
        project_id=project_id,     # 所属工程 ID（来自 URL 路径）
        role=body.role,            # 赋予的角色（Pydantic 已做格式校验）
    )
    db.add(access)  # 把 ORM 实例加入 session（等 commit 时写库）

    # ── 写审计日志 ──────────────────────────────────────────────────────────
    # log_audit：不自己 commit，和业务数据在同一个事务里原子提交
    await log_audit(
        db,
        actor_user_id=user.id,
        action=actions.PROJECT_MEMBER_ADD,       # "project_member.add"
        resource_type="project_member",
        resource_id=project_id,                  # 以 project_id 作为资源 ID
        metadata={
            "target_user_id": body.user_id,
            "role": body.role,
        },
        ip_address=request.client.host if request.client else None,
    )

    # ── 原子提交 ─────────────────────────────────────────────────────────────
    # UserProjectAccess + AuditLog 同一个事务里提交，保证原子性
    await db.commit()

    # ── 刷新并返回 ──────────────────────────────────────────────────────────
    # commit 后 ORM 对象可能"过期"，refresh 确保返回的是数据库里最新的值
    await db.refresh(access)
    return _to_direct_response(access)


# ─── 路由：PATCH /projects/{project_id}/members/{user_id}（改 role）────────

@router.patch(
    "/{project_id}/members/{user_id}",
    response_model=DirectMemberResponse,
    # require_project_role("owner")：只有 owner 才能改成员 role
    dependencies=[Depends(require_project_role("owner"))],
)
async def update_member_role(
    project_id: str,
    user_id: int,                            # FastAPI 从 URL path 提取 {user_id}，自动转 int
    body: MemberRoleUpdateRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DirectMemberResponse:
    """改 project 直接成员的 role（需 owner 权限）。

    保护规则（Last-owner 保护，仅限 direct owner）：
      - 如果被改的成员是 project 唯一的 direct owner，且新 role 不是 owner（降级），
        返回 422 "项目必须至少 1 个直接 owner"

    Args:
        project_id: 路径参数，目标工程 ID。
        user_id:    路径参数，目标成员用户 ID（整数）。
        body:       请求体（role）。
        request:    FastAPI Request，用于提取客户端 IP。
        user:       当前登录用户（操作者）。
        db:         异步 DB session。

    Returns:
        DirectMemberResponse：更新后的成员信息。

    Raises:
        HTTPException(404): 成员不存在（非直接成员）。
        HTTPException(422): 降级唯一 direct owner 被拒绝。
    """
    # ── 查直接成员记录是否存在 ────────────────────────────────────────────────
    # UserProjectAccess 复合主键：(user_id, project_id)
    access = await db.get(UserProjectAccess, (user_id, project_id))
    if not access:
        raise HTTPException(
            status_code=404,
            detail="成员不存在",
        )

    # ── Last-owner 保护（仅限 direct owner）──────────────────────────────────
    # 如果当前成员是 owner，且新 role 不是 owner（降级），才需要检查
    if access.role == "owner" and body.role != "owner":
        owner_count = await _count_direct_owners(project_id, db)
        if owner_count <= 1:
            # 唯一的 direct owner 被降级 → 违反约束
            raise HTTPException(
                status_code=422,
                detail="项目必须至少 1 个直接 owner",
            )

    # ── 记录变更（审计用）+ 更新字段 ──────────────────────────────────────────
    old_role = access.role     # 保存旧 role，用于审计日志
    access.role = body.role    # 直接赋值修改 ORM 对象属性（等 commit 时写库）

    # ── 写审计日志 ──────────────────────────────────────────────────────────
    await log_audit(
        db,
        actor_user_id=user.id,
        action=actions.PROJECT_MEMBER_ROLE_CHANGE,   # "project_member.role_change"
        resource_type="project_member",
        resource_id=project_id,
        metadata={
            "target_user_id": user_id,
            "old_role": old_role,
            "new_role": body.role,
        },
        ip_address=request.client.host if request.client else None,
    )

    # ── 原子提交 ─────────────────────────────────────────────────────────────
    await db.commit()

    # commit 后刷新，确保返回最新值
    await db.refresh(access)
    return _to_direct_response(access)


# ─── 路由：DELETE /projects/{project_id}/members/{user_id}（删成员）──────────

@router.delete(
    "/{project_id}/members/{user_id}",
    status_code=204,   # 成功删除返回 204 No Content（无响应体）
    # require_project_role("owner")：只有 owner 才能删成员
    dependencies=[Depends(require_project_role("owner"))],
)
async def remove_project_member(
    project_id: str,
    user_id: int,                            # FastAPI 从 URL path 提取 {user_id}，自动转 int
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """从 project 删除一个直接成员（需 owner 权限）。

    保护规则（Last-owner 保护，仅限 direct owner）：
      - 如果被删的是 project 唯一的 direct owner，返回 422
      - 即使 group 继承链里有 owner，也不影响 direct owner 的保护
        （因为继承不可删除单个项目层面的成员）

    Args:
        project_id: 路径参数，目标工程 ID。
        user_id:    路径参数，要删除的成员用户 ID（整数）。
        request:    FastAPI Request，用于提取客户端 IP。
        user:       当前登录用户（操作者）。
        db:         异步 DB session。

    Raises:
        HTTPException(404): 成员不存在（非直接成员）。
        HTTPException(422): 尝试删除 project 的唯一 direct owner。
    """
    # ── 查直接成员记录是否存在 ────────────────────────────────────────────────
    access = await db.get(UserProjectAccess, (user_id, project_id))
    if not access:
        raise HTTPException(
            status_code=404,
            detail="成员不存在",
        )

    # ── Last-owner 保护（仅限 direct owner）──────────────────────────────────
    if access.role == "owner":
        owner_count = await _count_direct_owners(project_id, db)
        if owner_count <= 1:
            # 这是最后一个 direct owner，禁止删除
            raise HTTPException(
                status_code=422,
                detail="项目必须至少 1 个直接 owner",
            )

    # ── 写审计日志（删除前记录，删后无法再查 role）────────────────────────────
    deleted_role = access.role  # 提前保存 role，删后无法访问
    await log_audit(
        db,
        actor_user_id=user.id,
        action=actions.PROJECT_MEMBER_REMOVE,    # "project_member.remove"
        resource_type="project_member",
        resource_id=project_id,
        metadata={
            "target_user_id": user_id,
            "role": deleted_role,
        },
        ip_address=request.client.host if request.client else None,
    )

    # ── 删除 ORM 对象 ──────────────────────────────────────────────────────
    # db.delete(access)：标记删除，等 commit 时执行 SQL DELETE
    await db.delete(access)

    # ── 原子提交 ─────────────────────────────────────────────────────────────
    # 审计日志 + 实际删除同一个事务里提交，保证原子性
    await db.commit()
