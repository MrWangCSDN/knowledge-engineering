"""Group CRUD 路由 (v2.0)。

接口列表（前缀 /groups）：
  - GET    /groups             列出当前用户可见的所有 group
  - POST   /groups             建 group（根组仅 Instance Admin；子组需父组 Owner）
  - GET    /groups/{gid}       查看单个 group（reporter+）
  - PATCH  /groups/{gid}       改 group 名称/描述（maintainer+）
  - DELETE /groups/{gid}       删 group（owner + 无子组 + 无工程）

关键约束：
  1. 嵌套深度 ≤ MAX_GROUP_DEPTH（= 3）
  2. 创建者自动成为 owner（group_members 表插一行）
  3. 循环检测：_group_depth 遍历父链时维护 visited 集合，防止 DB 层约束失效时死循环
  4. ID 格式：^[a-z][a-z0-9/\\-]*[a-z0-9]$（path-like，如 'retail-bank/credit-card'）

设计文档：[[多工程隔离与权限-设计]] (Obsidian)
"""

# from __future__ import annotations：启用"延迟注解求值"，让所有类型注解变为字符串，
# 避免 Python 3.9 以前版本里使用 list[str] / Optional[str] 等注解时的 NameError
from __future__ import annotations

from typing import Optional  # Optional[X] = Union[X, None]，表示字段可以是 None

# FastAPI 核心组件
# APIRouter：路由分组工具，prefix="/groups" 会自动加到每个路由路径前
# Depends：依赖注入标记，FastAPI 会自动调用并把结果注入参数
# HTTPException：向客户端返回 HTTP 错误响应（含 status_code + detail）
# Request：原始 HTTP 请求对象，用于获取客户端 IP 等信息
# status：HTTP 状态码常量（status.HTTP_201_CREATED 等）
from fastapi import APIRouter, Depends, HTTPException, Request, status

# Pydantic：数据验证 + 序列化框架
# BaseModel：请求/响应 Schema 基类，声明字段类型和校验规则
# Field：字段级校验参数（min_length、max_length、pattern 等）
from pydantic import BaseModel, Field

# SQLAlchemy 2.0 查询构造器
# select(Model.col).filter_by(...)：构造 SELECT SQL 语句
from sqlalchemy import select

# AsyncSession：异步数据库会话类型（用于类型注解 + await 调用）
from sqlalchemy.ext.asyncio import AsyncSession

# 项目内模块
from src.service.audit.logger import log_audit   # 统一审计日志写入（不 commit，由 caller 提交）
from src.service.audit import actions             # 审计动作常量（actions.GROUP_CREATE 等）
from src.service.auth_dependencies import get_current_user  # FastAPI dependency：JWT → User
from src.service.auth_models import User          # 用户 ORM 模型（users 表）
from src.service.db import get_db                 # FastAPI dependency：提供 DB session
from src.service.db_models_groups import Group, GroupMember  # Group / GroupMember ORM 模型
from src.service.db_models_homepage import Project           # Project ORM（判断有无关联工程）
from src.service.permission_deps import (
    ROLE_RANK,             # role 等级字典：{'reporter': 1, 'maintainer': 2, 'owner': 3}
    require_group_role,    # dependency 工厂：检查 role ≥ min_role
    resolve_group_role,    # 计算用户在 group 的继承 role（不含 Instance Admin 覆盖）
)


# ─── 常量 ──────────────────────────────────────────────────────────────────────

# MAX_GROUP_DEPTH：group 嵌套最大深度（包含根层），设计约束来自 spec
# 例：root(1) → lv1(2) → lv2(3) = 合法；再建子 → 第 4 层 = 拒绝
MAX_GROUP_DEPTH = 3


# ─── Router 定义 ──────────────────────────────────────────────────────────────

# APIRouter：FastAPI 的路由分组工具
# prefix="/groups"：所有路由路径前自动加上 "/groups"
# tags=["groups"]：在 OpenAPI 文档里把这些接口归到 "groups" 分类下
router = APIRouter(prefix="/groups", tags=["groups"])


# ─── Pydantic 请求 / 响应 Schema ──────────────────────────────────────────────

class GroupCreateRequest(BaseModel):
    """POST /groups 请求体。

    Attributes:
        id: 业务可读 group ID，格式 path-like（如 'retail-bank' 或 'retail-bank/credit-card'）。
            正则约束：^[a-z][a-z0-9/\\-]*[a-z0-9]$
            - 以小写字母开头
            - 中间可含小写字母、数字、斜杠（/）、连字符（-）
            - 以小写字母或数字结尾
        name: 人类可读的显示名，1~128 字符。
        description: 可选描述，最长 512 字符。
        parent_group_id: 父组 ID；None 表示建根 group。
    """
    # Field(...)：必填字段（... 是 Pydantic 的"required"标记）
    # pattern：Pydantic v2 的正则约束（Pydantic v1 用 regex=）
    id: str = Field(
        ...,
        min_length=2,
        max_length=64,
        pattern=r"^[a-z][a-z0-9/\-]*[a-z0-9]$",
    )
    name: str = Field(..., min_length=1, max_length=128)
    description: Optional[str] = Field(None, max_length=512)  # 可选，默认 None
    parent_group_id: Optional[str] = None  # 不传 = 根 group，传 = 子 group


class GroupUpdateRequest(BaseModel):
    """PATCH /groups/{gid} 请求体。

    所有字段都是可选的（Optional），只传需要改的字段。
    如果字段值为 None，表示不改该字段（不是清空）。

    Attributes:
        name: 新名称，最长 128 字符。
        description: 新描述，最长 512 字符。
    """
    name: Optional[str] = Field(None, max_length=128)
    description: Optional[str] = Field(None, max_length=512)


class GroupResponse(BaseModel):
    """GET/POST/PATCH /groups/* 响应体。

    Attributes:
        id: group 的业务 ID。
        name: 显示名。
        description: 描述（可能为 None）。
        parent_group_id: 父组 ID（根 group 为 None）。
        created_at: 创建时间，ISO8601 字符串（如 '2026-05-12T08:00:00'）。
    """
    id: str
    name: str
    description: Optional[str]
    parent_group_id: Optional[str]
    created_at: str


# ─── 内部工具函数 ─────────────────────────────────────────────────────────────

async def _group_depth(group_id: str, db: AsyncSession) -> int:
    """计算指定 group 的嵌套深度（根 group = 1，子 group = parent_depth + 1）。

    算法：沿 parent_group_id 向上遍历，统计跳数。
    防循环：维护 visited 集合，重复 ID 立即抛 500（DB 约束失效时的防御）。
    防无限循环：depth > MAX_GROUP_DEPTH * 2 时截断并报错。

    Args:
        group_id: 要计算深度的 group ID。
        db: 异步数据库 session。

    Returns:
        int：深度（根 group = 1，最深合法 = MAX_GROUP_DEPTH = 3）。

    Raises:
        HTTPException(500): 检测到循环依赖或异常深度（DB 约束失效时的防御线）。
    """
    depth = 1          # 初始深度：当前 group 本身算第 1 层
    cur = group_id     # 当前遍历到的 group id
    visited = {cur}    # 已访问过的 group id 集合，防止循环引用

    # while True：无限循环，靠内部 return / raise 退出
    while True:
        # db.scalar(select(Model.col).filter_by(...))：
        # 查询单个标量值（parent_group_id 列），等价于：
        # SELECT parent_group_id FROM groups WHERE id = cur LIMIT 1
        parent = await db.scalar(
            select(Group.parent_group_id).filter_by(id=cur)
        )

        if parent is None:
            # 到达根 group（parent_group_id IS NULL），返回当前深度
            return depth

        if parent in visited:
            # 循环依赖：parent 已经在路径上（DB 的 RESTRICT 约束应该防止这种情况，
            # 但保留这里作为防御性检查）
            raise HTTPException(
                status_code=500,
                detail="检测到 group 循环依赖",
            )

        visited.add(parent)  # 把 parent 加入已访问集合
        depth += 1           # 深度 +1
        cur = parent         # 向上走一层

        # 防御性截断：depth 超过 MAX_GROUP_DEPTH * 2 时异常
        # 正常路径不应触及（因为建组时已有深度校验），这里是双重保险
        if depth > MAX_GROUP_DEPTH * 2:
            raise HTTPException(
                status_code=500,
                detail="嵌套异常：深度超过预期上限",
            )


def _to_response(group: Group) -> GroupResponse:
    """将 Group ORM 实例转换为 GroupResponse Pydantic Schema。

    DRY 原则：统一 GET / POST / PATCH 的返回格式，避免重复代码。

    Args:
        group: Group ORM 实例（从数据库取出的行）。

    Returns:
        GroupResponse：标准化响应体。
    """
    return GroupResponse(
        id=group.id,
        name=group.name,
        description=group.description,
        parent_group_id=group.parent_group_id,
        # .isoformat()：datetime → ISO8601 字符串（如 '2026-05-12T08:00:00'）
        created_at=group.created_at.isoformat(),
    )


# ─── 路由：POST /groups（建 group）────────────────────────────────────────────

@router.post(
    "",                             # 路径为空 = /groups（前缀已在 APIRouter 里设置）
    response_model=GroupResponse,   # 自动将返回值序列化为 GroupResponse JSON
    status_code=201,                # 创建成功返回 201 Created
)
async def create_group(
    body: GroupCreateRequest,       # 请求体，FastAPI 自动从 JSON 解析 + 校验
    request: Request,               # 原始请求对象，用于获取客户端 IP
    user: User = Depends(get_current_user),    # 依赖注入：当前登录用户
    db: AsyncSession = Depends(get_db),        # 依赖注入：当前请求的 DB session
) -> GroupResponse:
    """建 group。

    权限规则：
      - 建根 group（parent_group_id=None）：仅 Instance Admin（is_admin=True）
      - 建子 group（parent_group_id=X）：父组 Owner（或 Instance Admin）

    创建后：
      - 创建者自动成为该 group 的 owner（group_members 表插一行）
      - 操作写审计日志

    Args:
        body: 请求体（id / name / description / parent_group_id）。
        request: FastAPI Request 对象，用于提取客户端 IP 写审计。
        user: 当前登录用户（由 JWT 解析）。
        db: 当前请求的异步 DB session。

    Returns:
        GroupResponse：新建 group 的元数据。

    Raises:
        HTTPException(403): 权限不足（非 admin 建根组，或非父组 owner 建子组）。
        HTTPException(404): 父组不存在。
        HTTPException(409): Group ID 已存在（重复）。
        HTTPException(422): 嵌套深度超过 MAX_GROUP_DEPTH。
    """
    # ── 权限校验 ──────────────────────────────────────────────────────────────

    if body.parent_group_id is None:
        # 建根 group：仅 Instance Admin 有权
        # user.is_admin 是 User ORM 属性，True 表示 Instance Admin
        if not user.is_admin:
            raise HTTPException(
                status_code=403,
                detail="仅 Instance Admin 能建根 group",
            )
    else:
        # 建子 group：先查父组是否存在
        # db.get(Model, pk)：按主键查询单条记录，比 select 更简洁
        parent = await db.get(Group, body.parent_group_id)
        if not parent:
            raise HTTPException(
                status_code=404,
                detail="父组不存在",
            )

        # 普通用户需要是父组的 owner（Instance Admin 跳过此检查）
        if not user.is_admin:
            # resolve_group_role：计算用户在父组的最终 role（含继承链）
            role = await resolve_group_role(user.id, body.parent_group_id, db)
            # role != 'owner'：必须是 owner，maintainer/reporter 不够
            if role != "owner":
                raise HTTPException(
                    status_code=403,
                    detail="需要父组 Owner 权限",
                )

        # ── 嵌套深度校验 ───────────────────────────────────────────────────
        # 父组的深度 + 1 = 新子组的深度；不能超过 MAX_GROUP_DEPTH
        parent_depth = await _group_depth(body.parent_group_id, db)
        if parent_depth + 1 > MAX_GROUP_DEPTH:
            raise HTTPException(
                status_code=422,
                detail=f"嵌套深度超过 {MAX_GROUP_DEPTH} 层",
            )

    # ── 去重校验（相同 ID 返回 409）────────────────────────────────────────
    # db.get(Group, body.id)：如果 ID 已存在，返回 Group 对象；不存在返回 None
    if await db.get(Group, body.id):
        raise HTTPException(
            status_code=409,
            detail="组 ID 已存在",
        )

    # ── 创建 Group ORM 实例 ─────────────────────────────────────────────────
    group = Group(
        id=body.id,
        name=body.name,
        description=body.description,
        parent_group_id=body.parent_group_id,
        created_by_user_id=user.id,
    )
    db.add(group)  # 把 ORM 实例加入 session（此时还未写库，等 commit）

    # ── 创建者自动成为 owner ─────────────────────────────────────────────────
    # GroupMember：group_members 表的 ORM 模型，复合主键 (user_id, group_id)
    db.add(GroupMember(
        user_id=user.id,          # 创建者
        group_id=group.id,        # 刚创建的 group
        role="owner",             # 最高权限
        added_by_user_id=user.id, # 自己加自己（创建时）
    ))

    # ── 写审计日志 ──────────────────────────────────────────────────────────
    # log_audit：不自己 commit，和业务数据在同一个事务里原子提交
    await log_audit(
        db,
        actor_user_id=user.id,
        action=actions.GROUP_CREATE,           # "group.create"
        resource_type="group",
        resource_id=group.id,
        metadata={
            "name": group.name,
            "parent_group_id": group.parent_group_id,
        },
        # request.client.host：客户端 IP；request.client 可能是 None（测试/反向代理场景）
        ip_address=request.client.host if request.client else None,
    )

    # ── 原子提交 ─────────────────────────────────────────────────────────────
    # Group + GroupMember + AuditLog 三张表的写操作在同一个事务里提交
    await db.commit()

    # ── 返回 ──────────────────────────────────────────────────────────────
    # db.commit() 后 ORM 对象处于"过期"状态，需要 refresh 才能访问属性
    # （SQLAlchemy 2.0 默认 expire_on_commit=True；测试 fixture 设 False，生产代码显式 refresh）
    await db.refresh(group)
    return _to_response(group)


# ─── 路由：GET /groups（列可见 groups）────────────────────────────────────────

@router.get(
    "",
    response_model=list[GroupResponse],  # 返回 GroupResponse 数组
)
async def list_visible_groups(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[GroupResponse]:
    """列出当前用户能看到的所有 group（含通过继承能看到的子孙 group）。

    规则：
      - Instance Admin：看到所有 group
      - 普通用户：看到自己有成员关系（含继承）的所有 group

    Args:
        user: 当前登录用户。
        db: 当前请求的异步 DB session。

    Returns:
        GroupResponse 列表（可能为空）。
    """
    if user.is_admin:
        # Instance Admin 可见所有 group
        # db.scalars(select(Group))：SELECT * FROM groups，返回 Group 对象游标
        # .all()：把游标里所有行一次取出，返回 list
        groups = (await db.scalars(select(Group))).all()
    else:
        # 普通用户：找出用户能访问的所有 group（直接成员 + 子孙 BFS）
        # _expand_user_groups 是 permission_deps.py 里的内部函数，直接导入
        from src.service.permission_deps import _expand_user_groups
        gids = await _expand_user_groups(user.id, db)

        if not gids:
            return []  # 不是任何 group 的成员，返回空列表

        # 批量查询（一次 SELECT IN，比多次 SELECT 高效）
        # Group.id.in_(gids)：生成 WHERE id IN (...) SQL
        groups = (await db.scalars(
            select(Group).filter(Group.id.in_(gids))
        )).all()

    # 列表推导式：对每个 Group ORM 对象调用 _to_response 转为 Pydantic Schema
    return [_to_response(g) for g in groups]


# ─── 路由：GET /groups/{group_id}（查看单个 group）────────────────────────────

@router.get(
    "/{group_id}",
    response_model=GroupResponse,
    # dependencies 列表里的 Depends 只做副作用（权限检查），不注入参数
    # require_group_role("reporter")：reporter+ 才能查看
    dependencies=[Depends(require_group_role("reporter"))],
)
async def get_group(
    group_id: str,                      # FastAPI 从 URL path 自动提取 {group_id}
    db: AsyncSession = Depends(get_db),
) -> GroupResponse:
    """查看单个 group 详情（需 reporter+ 权限）。

    Args:
        group_id: 路径参数，group 的业务 ID。
        db: 异步 DB session。

    Returns:
        GroupResponse：group 详情。

    Raises:
        HTTPException(404): group 不存在。
        HTTPException(403): 无权限（reporter 以下）。
    """
    group = await db.get(Group, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="组不存在")
    return _to_response(group)


# ─── 路由：PATCH /groups/{group_id}（改名/改描述）─────────────────────────────

@router.patch(
    "/{group_id}",
    response_model=GroupResponse,
    # maintainer+：维护者及以上才能改
    dependencies=[Depends(require_group_role("maintainer"))],
)
async def update_group(
    group_id: str,
    body: GroupUpdateRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> GroupResponse:
    """改 group 的名称或描述（需 maintainer+ 权限）。

    只改有传入值的字段（None 的字段不做更新）。
    有实际改动时写审计日志；无改动时跳过审计。

    Args:
        group_id: 路径参数，目标 group 的业务 ID。
        body: 请求体（name / description，都可选）。
        request: FastAPI Request，用于提取客户端 IP。
        user: 当前登录用户。
        db: 异步 DB session。

    Returns:
        GroupResponse：更新后的 group 详情。

    Raises:
        HTTPException(404): group 不存在。
        HTTPException(403): 权限不足（reporter 角色）。
    """
    group = await db.get(Group, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="组不存在")

    # changes：记录本次改动（旧值 → 新值），用于审计日志
    # dict 字面量：{key: value} 语法；这里初始化为空，按需填入
    changes: dict = {}

    # 只有字段有传入值 + 值确实不同时，才做更新（避免无意义的 UPDATE SQL）
    if body.name is not None and body.name != group.name:
        # 记录变更详情：旧值和新值
        changes["name"] = (group.name, body.name)
        group.name = body.name  # 直接赋值修改 ORM 对象属性

    if body.description is not None and body.description != group.description:
        changes["description"] = (group.description, body.description)
        group.description = body.description

    if changes:
        # 有实际改动 → 写审计日志
        await log_audit(
            db,
            actor_user_id=user.id,
            action=actions.GROUP_UPDATE,
            resource_type="group",
            resource_id=group_id,
            metadata={
                # 字典推导式：{k: {"old": v[0], "new": v[1]} for k, v in changes.items()}
                # 对 changes 里的每一对 (旧值, 新值) 展开为 {"old": 旧值, "new": 新值}
                "changes": {k: {"old": v[0], "new": v[1]} for k, v in changes.items()}
            },
            ip_address=request.client.host if request.client else None,
        )

    await db.commit()

    # commit 后刷新 ORM 对象，确保返回的是数据库里最新的值
    await db.refresh(group)
    return _to_response(group)


# ─── 路由：DELETE /groups/{group_id}（删 group）──────────────────────────────

@router.delete(
    "/{group_id}",
    status_code=204,  # 成功删除返回 204 No Content（无响应体）
    # owner：只有 owner 才能删 group
    dependencies=[Depends(require_group_role("owner"))],
)
async def delete_group(
    group_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """删 group（需 owner 权限 + 无子组 + 无工程）。

    规则：
      - group 不存在 → 404
      - 有子组 → 403（"先删子组"）
      - 有关联工程 → 403（"先迁移工程"）
      - 成功 → 204 No Content

    Args:
        group_id: 路径参数，目标 group 的业务 ID。
        request: FastAPI Request，用于提取客户端 IP。
        user: 当前登录用户（must be owner）。
        db: 异步 DB session。

    Raises:
        HTTPException(404): group 不存在。
        HTTPException(403): 有子组 / 有工程 / 权限不足（由 require_group_role 处理）。
    """
    group = await db.get(Group, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="组不存在")

    # ── 校验：无子组 ────────────────────────────────────────────────────────
    # select(Group.id).filter_by(parent_group_id=group_id).limit(1)：
    # 只查第一个子组 ID（.limit(1)），够判断"有没有"就行，不需要查全部
    # db.scalar：返回标量值（字符串 ID），没有则返回 None
    sub_count = await db.scalar(
        select(Group.id).filter_by(parent_group_id=group_id).limit(1)
    )
    if sub_count:
        raise HTTPException(status_code=403, detail="先删子组")

    # ── 校验：无工程 ────────────────────────────────────────────────────────
    # 查 projects 表里 group_id = 当前 group_id 的第一条记录
    proj_count = await db.scalar(
        select(Project.id).filter_by(group_id=group_id).limit(1)
    )
    if proj_count:
        raise HTTPException(status_code=403, detail="先迁移工程")

    # ── 写审计日志（在删除之前记录，因为删后 group 对象已标记删除）────────
    await log_audit(
        db,
        actor_user_id=user.id,
        action=actions.GROUP_DELETE,
        resource_type="group",
        resource_id=group_id,
        metadata={"name": group.name},  # 删除后无法再查名称，提前记录
        ip_address=request.client.host if request.client else None,
    )

    # ── 删除 ORM 对象 ──────────────────────────────────────────────────────
    # db.delete(group)：标记删除，等 commit 时执行 SQL DELETE
    # 注意：GroupMember 有 cascade="all, delete-orphan"，级联删除成员记录
    await db.delete(group)

    # ── 原子提交 ─────────────────────────────────────────────────────────────
    # 审计日志 + 实际删除同一个事务里提交，保证原子性
    await db.commit()
