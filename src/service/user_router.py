"""User Management CRUD 路由 (v2.0, Task 10)。

接口列表（前缀 /admin/users）：
  - GET    /admin/users              列所有用户（支持 filter）
  - POST   /admin/users              创建用户（含可选 is_admin）
  - PATCH  /admin/users/{uid}        改 is_admin / is_active / 改密码
  - DELETE /admin/users/{uid}        删用户（先检查 group/project ownership）

全部接口均需 Depends(get_current_admin)（仅 Instance Admin 可访问）。

关键保护：
  1. 不能降级最后 1 个 Instance Admin（PATCH is_admin=False 时检查）→ 422
  2. 删用户前：
     - 有 group owner 角色 → 422 "用户是 N 个组的 owner，先转让"
     - 有 project owner 角色 → 422 "用户是 N 个工程的 owner，先转让"
     - 有凭证 → SET NULL FK 自动处理，不阻塞
  3. 改密码：用 auth_security.hash_password 加密
  4. 每个 mutation 写审计：USER_CREATE / USER_UPDATE / USER_DELETE
     + USER_SET_ADMIN / USER_ACTIVATE / USER_DEACTIVATE（按改动类型细分）

设计文档：[[多工程隔离与权限-设计]] (Obsidian)
"""

# from __future__ import annotations：启用"延迟注解求值"，让 Optional / list 等注解
# 在 Python 3.9 以前的版本也能正常工作（无需 from typing import List 等）
from __future__ import annotations

from typing import Optional  # Optional[X] = Union[X, None]，表示字段可以是 None

# FastAPI 核心组件
# APIRouter：路由分组工具，prefix="/admin/users" 会自动加到每个路由路径前
# Depends：依赖注入标记，FastAPI 自动调用并把结果注入参数
# HTTPException：向客户端返回带 HTTP 状态码的错误响应（含 status_code + detail）
# Request：原始 HTTP 请求对象，用于获取客户端 IP 写审计
# status：HTTP 状态码常量（status.HTTP_201_CREATED 等）
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

# Pydantic：数据验证 + 序列化框架
# BaseModel：请求/响应 Schema 基类，声明字段类型和校验规则
# Field：字段级校验参数（min_length、max_length、pattern 等）
from pydantic import BaseModel, EmailStr, Field

# SQLAlchemy 2.0 查询构造器
# func：SQL 聚合函数（func.count）
# select：构造 SELECT 查询语句
from sqlalchemy import func, select

# AsyncSession：异步数据库会话类型
from sqlalchemy.ext.asyncio import AsyncSession

# 项目内模块
from src.service.audit.logger import log_audit   # 统一审计日志写入（不 commit）
from src.service.audit import actions             # 审计动作常量
from src.service.auth_dependencies import get_current_admin  # Depends：仅 admin 通过
from src.service.auth_models import User          # 用户 ORM 模型（users 表）
from src.service.auth_security import hash_password   # bcrypt 密码加密
from src.service.db import get_db                 # FastAPI dependency：提供 DB session
from src.service.db_models_groups import GroupMember  # group 成员 ORM 模型
from src.service.db_models_homepage import UserProjectAccess  # project 成员 ORM 模型
from src.service.deps_infra import require_infra_healthy  # 设计 §3.3：基础设施不可用时返 503


# ─── Router 定义 ──────────────────────────────────────────────────────────────

# APIRouter：FastAPI 路由分组工具
# prefix="/admin/users"：所有路由路径前自动加上 "/admin/users"
# tags=["admin-users"]：在 OpenAPI 文档里把这些接口归到 "admin-users" 分类下
router = APIRouter(
    prefix="/admin/users",
    tags=["admin-users"],
    # 设计 §3.3：任一 critical 依赖挂 → 503 INFRA_UNHEALTHY
    dependencies=[Depends(require_infra_healthy)],
)


# ─── Pydantic 请求 / 响应 Schema ──────────────────────────────────────────────

class UserCreateRequest(BaseModel):
    """POST /admin/users 请求体。

    Attributes:
        email:    用户邮箱，全局唯一。
        username: 登录用户名，全局唯一，1~100 字符。
        password: 明文密码，至少 8 字符（后端存 bcrypt hash）。
        is_admin: 是否为 Instance Admin，默认 False。
    """
    # EmailStr：Pydantic 内置邮箱格式校验类型（需要 email-validator 包）
    email: EmailStr
    # Field(...)：... 表示"必填"（Pydantic 的 required 标记）
    username: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=8)  # 明文密码，写库前 hash
    is_admin: bool = False  # 默认非 admin；可选传 True 直接创建 admin 用户


class UserUpdateRequest(BaseModel):
    """PATCH /admin/users/{uid} 请求体。

    所有字段都是可选的（Optional），只传需要改的字段。
    None 表示不改该字段（不是清空）。

    Attributes:
        is_admin:  是否为 Instance Admin；降级最后 admin 时报 422。
        is_active: 是否激活；False = 停用（禁止登录但不删除）。
        password:  新明文密码，传入后后端 hash 再存库。
    """
    is_admin: Optional[bool] = None    # None = 不改
    is_active: Optional[bool] = None   # None = 不改
    password: Optional[str] = Field(None, min_length=8)  # None = 不改密码


class UserResponse(BaseModel):
    """GET/POST/PATCH /admin/users/* 响应体。

    不返回 hashed_password（安全原则：密码 hash 永不出 API）。

    Attributes:
        id:         用户整数 ID。
        email:      用户邮箱。
        username:   登录用户名。
        is_active:  是否激活。
        is_admin:   是否为 Instance Admin。
        created_at: 创建时间，ISO8601 字符串。
    """
    id: int
    email: str
    username: str
    is_active: bool
    is_admin: bool
    created_at: str  # datetime.isoformat() 转字符串，方便 JSON 序列化


# ─── 内部工具函数 ──────────────────────────────────────────────────────────────

def _to_response(user: User) -> UserResponse:
    """将 User ORM 实例转换为 UserResponse Pydantic Schema。

    DRY 原则：统一 GET / POST / PATCH 的返回格式，避免重复代码。

    Args:
        user: User ORM 实例（从数据库取出的行）。

    Returns:
        UserResponse：标准化响应体（不含密码 hash）。
    """
    return UserResponse(
        id=user.id,
        email=user.email,
        username=user.username,
        is_active=user.is_active,
        is_admin=user.is_admin,
        # .isoformat()：datetime → ISO8601 字符串（如 '2026-05-12T08:00:00'）
        created_at=user.created_at.isoformat(),
    )


async def _count_admins(db: AsyncSession) -> int:
    """统计当前 Instance Admin 数量。

    用于"最后 1 个 Instance Admin 保护"：
    - PATCH is_admin=False 且当前 admin 数量 = 1 时 → 422
    - DELETE 且用户是唯一 admin 时（按需判断）

    Args:
        db: 异步数据库 session。

    Returns:
        int：is_admin=True 的用户数量（>= 0）。
    """
    # func.count(User.id)：SQL COUNT(id) 聚合，统计 admin 用户数
    # filter_by(is_admin=True)：WHERE is_admin = 1（SQLite 里 bool 存 0/1）
    count = await db.scalar(
        select(func.count(User.id)).filter_by(is_admin=True)
    )
    # db.scalar 对空结果返回 None；or 0 保证返回 int
    return count or 0


# ─── 路由：GET /admin/users（列所有用户）─────────────────────────────────────

@router.get(
    "",                              # 路径为空 = /admin/users（前缀已在 APIRouter 设置）
    response_model=list[UserResponse],  # 返回 UserResponse 数组
)
async def list_users(
    # Query(None, ...)：可选的 URL 查询参数，默认 None
    username: Optional[str] = Query(None, description="按用户名模糊过滤（contains）"),
    is_admin: Optional[bool] = Query(None, description="按 is_admin 过滤"),
    is_active: Optional[bool] = Query(None, description="按 is_active 过滤"),
    # Depends(get_current_admin)：仅 Instance Admin 通过，否则 403
    _admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> list[UserResponse]:
    """列出所有用户，支持 filter（仅 Instance Admin 可访问）。

    支持过滤参数：
      - username：模糊匹配（LIKE %xxx%）
      - is_admin：精确匹配
      - is_active：精确匹配

    Args:
        username: 可选，按用户名模糊过滤。
        is_admin: 可选，按 admin 状态过滤。
        is_active: 可选，按激活状态过滤。
        _admin:   Instance Admin 依赖（只做权限校验，不使用其值）。
        db:       异步 DB session。

    Returns:
        UserResponse 列表（按创建时间升序，可能为空）。
    """
    # 从 select(User) 开始构建查询；后续按需 .where() 附加过滤条件
    # select(User)：SELECT * FROM users（返回完整 User ORM 对象）
    stmt = select(User)

    # 按用户名模糊过滤
    # User.username.contains(username)：SQLAlchemy 的 LIKE %username% 快捷方法
    if username is not None:
        stmt = stmt.where(User.username.contains(username))

    # 按 is_admin 精确过滤
    # User.is_admin == is_admin：SQLAlchemy 的等值过滤（== 是 Python 的 == 运算符，SQLAlchemy 重载为 SQL =）
    if is_admin is not None:
        stmt = stmt.where(User.is_admin == is_admin)

    # 按 is_active 精确过滤
    if is_active is not None:
        stmt = stmt.where(User.is_active == is_active)

    # .order_by(User.id)：按 id 升序排列（创建时间升序的近似，id 自增）
    stmt = stmt.order_by(User.id)

    # db.scalars(stmt).all()：执行查询，把所有 User 对象一次取出为列表
    users = (await db.scalars(stmt)).all()

    # 列表推导式：对每个 User ORM 对象调用 _to_response 转为 Pydantic Schema
    return [_to_response(u) for u in users]


# ─── 路由：POST /admin/users（创建用户）──────────────────────────────────────

@router.post(
    "",
    response_model=UserResponse,
    status_code=201,  # 创建成功返回 201 Created
)
async def create_user(
    body: UserCreateRequest,                          # 请求体，FastAPI 自动解析 + 校验
    request: Request,                                 # 原始请求，用于提取客户端 IP
    admin: User = Depends(get_current_admin),         # 操作人（Instance Admin）
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    """创建新用户（仅 Instance Admin 可操作）。

    规则：
      - email 必须全局唯一（重复 → 409）
      - username 必须全局唯一（重复 → 409）
      - is_admin 默认 False，可选传 True 直接创建 admin 用户
      - 密码用 bcrypt hash 存库（明文不落盘）
      - 写审计日志 USER_CREATE（+ USER_SET_ADMIN 如果 is_admin=True）

    Args:
        body:    请求体（email / username / password / is_admin）。
        request: FastAPI Request，用于提取客户端 IP 写审计。
        admin:   当前 Instance Admin 用户（操作者）。
        db:      异步 DB session。

    Returns:
        UserResponse：新建用户的信息（不含密码 hash）。

    Raises:
        HTTPException(409): email 或 username 已被占用。
    """
    # ── email 唯一性检查 ─────────────────────────────────────────────────────
    # db.scalar(select(User.id).filter_by(email=...))：
    # 只查 id 列（比 * 轻量），判断存在性
    existing_email = await db.scalar(
        select(User.id).filter_by(email=body.email)
    )
    if existing_email is not None:
        raise HTTPException(
            status_code=409,
            detail="邮箱已被注册",
        )

    # ── username 唯一性检查 ──────────────────────────────────────────────────
    existing_username = await db.scalar(
        select(User.id).filter_by(username=body.username)
    )
    if existing_username is not None:
        raise HTTPException(
            status_code=409,
            detail="用户名已被占用",
        )

    # ── 创建 User ORM 实例 ────────────────────────────────────────────────────
    # hash_password：auth_security 里的 bcrypt hash 函数（cost factor = 12）
    # 明文密码不存库，只存 hash
    user = User(
        email=body.email,
        username=body.username,
        hashed_password=hash_password(body.password),  # bcrypt hash，明文不落盘
        is_active=True,           # 创建时默认激活
        is_admin=body.is_admin,   # 按请求体设置（默认 False）
    )
    db.add(user)  # 加入 session（等 commit 才写库）

    # ── 写审计日志 ────────────────────────────────────────────────────────────
    # await db.flush()：把 ORM 对象同步到数据库（分配自增 id），但不提交事务
    # 这样 user.id 就有值了，可以用来写审计日志
    await db.flush()

    # USER_CREATE 审计：记录操作者、新用户邮箱/用户名/admin 状态
    await log_audit(
        db,
        actor_user_id=admin.id,
        action=actions.USER_CREATE,
        resource_type="user",
        resource_id=str(user.id),
        metadata={
            "email": user.email,
            "username": user.username,
            "is_admin": user.is_admin,
        },
        ip_address=request.client.host if request.client else None,
    )

    # 如果创建的是 admin 用户，额外写一条 USER_SET_ADMIN 审计
    if user.is_admin:
        await log_audit(
            db,
            actor_user_id=admin.id,
            action=actions.USER_SET_ADMIN,
            resource_type="user",
            resource_id=str(user.id),
            metadata={"is_admin": True, "reason": "created_as_admin"},
            ip_address=request.client.host if request.client else None,
        )

    # ── 原子提交 ─────────────────────────────────────────────────────────────
    # User + AuditLog 同一个事务里提交，保证原子性
    await db.commit()

    # ── 刷新并返回 ────────────────────────────────────────────────────────────
    # commit 后 ORM 对象"过期"（expire_on_commit=True），refresh 确保 created_at 等已读回
    await db.refresh(user)
    return _to_response(user)


# ─── 路由：PATCH /admin/users/{uid}（改 is_admin / is_active / 密码）────────

@router.patch(
    "/{uid}",
    response_model=UserResponse,
)
async def update_user(
    uid: int,                                         # 路径参数，目标用户的整数 ID
    body: UserUpdateRequest,
    request: Request,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    """修改用户属性（仅 Instance Admin 可操作）。

    可修改字段：
      - is_admin：授予或撤销 Instance Admin 权限
        约束：不能降级最后 1 个 Instance Admin → 422
      - is_active：激活或停用用户账号
      - password：修改密码（bcrypt hash 后存库）

    每类修改各写一条细粒度审计日志。

    Args:
        uid:     路径参数，目标用户的整数 ID。
        body:    请求体（is_admin / is_active / password，都可选）。
        request: FastAPI Request，用于提取客户端 IP。
        admin:   当前 Instance Admin 用户（操作者）。
        db:      异步 DB session。

    Returns:
        UserResponse：更新后的用户信息。

    Raises:
        HTTPException(404): 用户不存在。
        HTTPException(422): 降级最后一个 Instance Admin 被拒绝。
    """
    # ── 查目标用户是否存在 ───────────────────────────────────────────────────
    # db.get(Model, pk)：按主键查询，比 select+filter 更简洁，返回 ORM 对象或 None
    user = await db.get(User, uid)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    # changes：记录本次改动内容，后续按类型写审计日志
    # dict 字面量，初始为空，按需填入
    ip = request.client.host if request.client else None

    # ── 处理 is_admin 变更 ──────────────────────────────────────────────────
    if body.is_admin is not None and body.is_admin != user.is_admin:
        # 降级（is_admin: True → False）时，检查是否是最后一个 admin
        if not body.is_admin:
            # _count_admins 统计所有 is_admin=True 的用户数
            admin_count = await _count_admins(db)
            if admin_count <= 1:
                # 这是最后一个 Instance Admin，禁止降级
                raise HTTPException(
                    status_code=422,
                    detail="不能降级最后一个 Instance Admin",
                )

        # 记录旧值，更新字段
        old_is_admin = user.is_admin
        user.is_admin = body.is_admin

        # 写细粒度审计：USER_SET_ADMIN（含新旧值）
        await log_audit(
            db,
            actor_user_id=admin.id,
            action=actions.USER_SET_ADMIN,
            resource_type="user",
            resource_id=str(uid),
            metadata={
                "old_is_admin": old_is_admin,
                "new_is_admin": body.is_admin,
            },
            ip_address=ip,
        )

    # ── 处理 is_active 变更 ─────────────────────────────────────────────────
    if body.is_active is not None and body.is_active != user.is_active:
        old_is_active = user.is_active
        user.is_active = body.is_active

        # 激活 vs 停用：写不同的审计 action
        # 激活：USER_ACTIVATE；停用：USER_DEACTIVATE
        audit_action = actions.USER_ACTIVATE if body.is_active else actions.USER_DEACTIVATE
        await log_audit(
            db,
            actor_user_id=admin.id,
            action=audit_action,
            resource_type="user",
            resource_id=str(uid),
            metadata={
                "old_is_active": old_is_active,
                "new_is_active": body.is_active,
            },
            ip_address=ip,
        )

    # ── 处理密码变更 ────────────────────────────────────────────────────────
    if body.password is not None:
        # hash_password：bcrypt hash，明文不落盘
        user.hashed_password = hash_password(body.password)

        # 写审计日志 USER_UPDATE（密码变更；不记录 hash 本身，只记录"changed"）
        await log_audit(
            db,
            actor_user_id=admin.id,
            action=actions.USER_UPDATE,
            resource_type="user",
            resource_id=str(uid),
            metadata={"field": "password", "changed": True},
            ip_address=ip,
        )

    # ── 原子提交 ─────────────────────────────────────────────────────────────
    # User 字段变更 + AuditLog 同一个事务里提交，保证原子性
    await db.commit()

    # commit 后刷新，确保返回的是数据库里最新的值
    await db.refresh(user)
    return _to_response(user)


# ─── 路由：DELETE /admin/users/{uid}（删用户）────────────────────────────────

@router.delete(
    "/{uid}",
    status_code=204,  # 成功删除返回 204 No Content（无响应体）
)
async def delete_user(
    uid: int,                              # 路径参数，目标用户的整数 ID
    request: Request,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    """删除用户（仅 Instance Admin 可操作）。

    删除前检查：
      1. 用户是 group owner → 422 "用户是 N 个组的 owner，先转让"
      2. 用户是 project owner（user_project_access）→ 422 "用户是 N 个工程的 owner，先转让"
      3. 用户有凭证 → SET NULL FK 自动处理，不阻塞（凭证数据保留）

    成功删除后：
      - 写审计日志 USER_DELETE
      - 返回 204 No Content

    Args:
        uid:     路径参数，目标用户的整数 ID。
        request: FastAPI Request，用于提取客户端 IP。
        admin:   当前 Instance Admin 用户（操作者）。
        db:      异步 DB session。

    Raises:
        HTTPException(404): 用户不存在。
        HTTPException(422): 用户是组 / 工程的 owner，先转让。
    """
    # ── 查目标用户是否存在 ───────────────────────────────────────────────────
    user = await db.get(User, uid)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    # ── 检查 group ownership ────────────────────────────────────────────────
    # 查用户在所有 group 里 role='owner' 的记录数
    # func.count(GroupMember.group_id)：SQL COUNT(group_id) 聚合
    # filter_by(user_id=uid, role="owner")：WHERE user_id=? AND role='owner'
    group_owner_count = await db.scalar(
        select(func.count(GroupMember.group_id)).filter_by(
            user_id=uid,
            role="owner",
        )
    ) or 0

    if group_owner_count > 0:
        # 用户仍是某些组的 owner，禁止删除
        raise HTTPException(
            status_code=422,
            detail=f"用户是 {group_owner_count} 个组的 owner，先转让",
        )

    # ── 检查 project ownership ──────────────────────────────────────────────
    # 查用户在 user_project_access 表里 role='owner' 的记录数
    project_owner_count = await db.scalar(
        select(func.count(UserProjectAccess.project_id)).filter_by(
            user_id=uid,
            role="owner",
        )
    ) or 0

    if project_owner_count > 0:
        # 用户仍是某些工程的 owner，禁止删除
        raise HTTPException(
            status_code=422,
            detail=f"用户是 {project_owner_count} 个工程的 owner，先转让",
        )

    # ── 凭证处理（不阻塞）──────────────────────────────────────────────────
    # GitCredential.owner_user_id 有 ondelete='SET NULL' FK，
    # 删用户时数据库自动将凭证的 owner_user_id 置 NULL，凭证数据本身保留。
    # Python 层不需要任何额外操作。

    # ── 写审计日志（删除前记录，删后无法再查用户信息）─────────────────────
    await log_audit(
        db,
        actor_user_id=admin.id,
        action=actions.USER_DELETE,
        resource_type="user",
        resource_id=str(uid),
        metadata={
            "email": user.email,
            "username": user.username,
        },
        ip_address=request.client.host if request.client else None,
    )

    # ── 删除 ORM 对象 ──────────────────────────────────────────────────────
    # db.delete(user)：标记删除，等 commit 时执行 SQL DELETE
    # GroupMember.user_id 有 ondelete='CASCADE' FK → 成员记录自动级联删除
    # UserProjectAccess.user_id 有 ondelete='CASCADE' FK → 访问记录自动级联删除
    await db.delete(user)

    # ── 原子提交 ─────────────────────────────────────────────────────────────
    # 审计日志 + 实际删除同一个事务里提交，保证原子性
    await db.commit()
