"""用户级凭证管理 (v2.0)。

每个用户管自己的 PAT（GitLab 风格）：
  - GET    /credentials             列我的所有凭证
  - POST   /credentials             建一条我的凭证
  - DELETE /credentials/{cred_id}   删我的某条凭证

Instance Admin 走 /admin/credentials/* 查看所有 / 强删（审计模式）。

设计文档：[[凭证管理-v2-设计]]
"""
from __future__ import annotations  # 延迟注解解析（PEP 563），类型注解字符串化，避免循环依赖

import uuid  # 标准库：生成唯一 ID（UUID4），用于凭证 id

from typing import Optional  # Optional[X] 表示值可以是 X 或 None

from fastapi import APIRouter, Depends, HTTPException, Request, status  # FastAPI 核心组件
from pydantic import BaseModel, Field  # Pydantic：数据验证 + 序列化（Request/Response schema）
from sqlalchemy import select  # SQLAlchemy 2.0 ORM 查询构造器
from sqlalchemy.ext.asyncio import AsyncSession  # 异步 SQLAlchemy session 类型

from src.service.audit.logger import log_audit  # 统一审计日志写入（不 commit，由 caller 一起 commit）
from src.service.audit import actions  # 审计动作常量（如 actions.CREDENTIAL_CREATE）
from src.service.auth_dependencies import get_current_user  # FastAPI Depends：解析 JWT → User
from src.service.auth_models import User  # ORM 用户模型（auth_models 里的 User 类）
from src.service.db import get_db  # FastAPI Depends：获取当前请求的 AsyncSession
from src.service.db_models_homepage import GitCredential  # ORM 模型：git_credentials 表
from src.service.token_crypto import encrypt_token, token_hint  # Fernet 加密 + UI hint 工具函数


# APIRouter：FastAPI 的路由分组工具，prefix 会自动加到下面每个路由的路径前
# tags 用于 OpenAPI 文档中的分组标签
router = APIRouter(prefix="/credentials", tags=["credentials"])


# ─── Pydantic 请求/响应 Schema ────────────────────────────────────────────────

class CredentialCreateRequest(BaseModel):
    """POST /credentials 请求体。

    Attributes:
        name: 凭证的人类可读名称，如 'My GitLab PAT'。长度 1~128。
        token: 明文 PAT；后端会用 Fernet 加密存储，前端永远拿不到原文。长度 8~512。
        type: 凭证类型，默认 'pat'；v2 预留扩展（未来支持 ssh_key）。
    """
    # Field(...)：表示必填字段；min_length/max_length 是 Pydantic 内置校验（422 如不满足）
    name: str = Field(..., min_length=1, max_length=128)
    token: str = Field(..., min_length=8, max_length=512)
    type: str = Field("pat", max_length=32)  # 默认 pat；默认值写在第一个参数


class CredentialResponse(BaseModel):
    """GET/POST /credentials 返回体。

    注意：永远不含 encrypted_token / 原始 token，
    前端只能看 token_hint（末 4 位加掩码，如 '****abc'）。

    Attributes:
        id: 凭证唯一 ID，如 'cred_abc123defg'。
        name: 凭证名称。
        type: 凭证类型（pat / ssh_key 等）。
        token_hint: 末 4 位掩码，仅 UI 展示用。
        created_at: 创建时间，ISO8601 字符串。
    """
    id: str
    name: str
    type: str
    token_hint: Optional[str]  # Optional = 可能为 None（历史数据或 token 太短时）
    created_at: str


# ─── 路由实现 ─────────────────────────────────────────────────────────────────

@router.get("", response_model=list[CredentialResponse])
async def list_my_credentials(
    user: User = Depends(get_current_user),  # Depends：FastAPI 依赖注入，解析 JWT 得到当前用户
    db: AsyncSession = Depends(get_db),       # Depends：注入当前请求的数据库 session
) -> list[CredentialResponse]:
    """列出当前用户自己的所有凭证。

    普通用户只能看到自己的凭证（owner_user_id == 当前用户 id）。
    如需查看所有用户的凭证，请使用 GET /admin/credentials（需 admin 权限）。

    Args:
        user: 当前登录用户（由 JWT 解析得到）。
        db: 当前请求的异步数据库 session。

    Returns:
        当前用户持有的凭证列表（不含 token 原文）。
    """
    # select(...).filter_by(...)：SQLAlchemy 2.0 ORM 查询
    # filter_by(owner_user_id=user.id)：等价于 WHERE owner_user_id = <当前用户 id>
    rows = (await db.scalars(
        select(GitCredential).filter_by(owner_user_id=user.id)
    )).all()  # .all()：把结果集转为 Python list

    # 列表推导式：对每条记录调用 _to_response 转为 Pydantic schema
    return [_to_response(c) for c in rows]


@router.post("", response_model=CredentialResponse, status_code=status.HTTP_201_CREATED)
async def create_my_credential(
    body: CredentialCreateRequest,             # 自动从请求 body 反序列化+校验
    request: Request,                          # FastAPI Request 对象，用于获取客户端 IP
    user: User = Depends(get_current_user),    # 当前登录用户
    db: AsyncSession = Depends(get_db),        # 异步 DB session
) -> CredentialResponse:
    """新建一条属于当前用户的凭证。

    PAT 明文用 Fernet 对称加密后存库（AES-128-CBC + HMAC-SHA256）。
    返回的 token_hint 是末 4 位加掩码，原文永远不出库。

    Args:
        body: 请求体（name / token / type）。
        request: FastAPI Request，用于提取客户端 IP 写审计日志。
        user: 当前登录用户。
        db: 当前请求的异步数据库 session。

    Returns:
        新建凭证的元数据（不含 token 原文）。
    """
    # 构造 GitCredential ORM 实例，准备写入 git_credentials 表
    cred = GitCredential(
        # uuid4().hex：生成 32 位随机十六进制字符串；[:12] 取前 12 位作为短 ID 后缀
        id="cred_" + uuid.uuid4().hex[:12],
        name=body.name,
        type=body.type,
        # encrypt_token：Fernet 加密明文 PAT，返回 base64 密文字符串
        encrypted_token=encrypt_token(body.token),
        # token_hint：生成末 4 位掩码（如 '****bc7f'），仅 UI 展示用，不影响解密
        token_hint=token_hint(body.token),
        # owner_user_id：关键！绑定凭证归属用户（v2.0 新增字段）
        owner_user_id=user.id,
        # created_by：存 username（人类可读），方便审计查阅时不需要 JOIN users 表
        created_by=user.username,
    )
    db.add(cred)  # 把 ORM 实例加入 session（此时还未写库，等 commit）

    # log_audit：写审计日志（同一个 session，和业务数据一起原子提交）
    # 审计失败只 warning，不中断业务（见 audit/logger.py 的设计原则）
    await log_audit(
        db,
        actor_user_id=user.id,                  # 操作人：当前用户
        action=actions.CREDENTIAL_CREATE,        # 动作常量："credential.create"
        resource_type="credential",             # 资源类型
        resource_id=cred.id,                    # 资源 ID（凭证 id）
        metadata={"name": cred.name, "type": cred.type},  # 附加上下文
        # request.client.host：客户端 IP；request.client 可能是 None（测试/proxy 场景）
        ip_address=request.client.host if request.client else None,
    )

    await db.commit()  # 原子提交：凭证 + 审计日志同时落库
    return _to_response(cred)


@router.delete("/{cred_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_credential(
    cred_id: str,                              # FastAPI 从路径参数提取
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """删除当前用户自己的某条凭证。

    - 凭证不存在 → 404
    - 凭证属于其他用户 → 403（不能删别人的）
    - 成功 → 204 No Content

    Args:
        cred_id: 路径参数，目标凭证 ID（如 'cred_abc123'）。
        request: FastAPI Request，用于提取客户端 IP 写审计日志。
        user: 当前登录用户。
        db: 当前请求的异步数据库 session。
    """
    # db.get(Model, pk)：按主键查询单条记录；异步版本
    cred = await db.get(GitCredential, cred_id)

    # 凭证不存在 → 404
    if not cred:
        raise HTTPException(status_code=404, detail="凭证不存在")

    # 所有权校验：owner_user_id 必须等于当前用户 id
    # != 而不是 is not，因为 owner_user_id 是 int，Python 中整数比较用 !=
    if cred.owner_user_id != user.id:
        raise HTTPException(status_code=403, detail="不是该凭证的所有者")

    await db.delete(cred)  # 标记删除（仍在 session 内，等 commit）

    # 删除前先写审计日志（和删除操作同一个事务，原子提交）
    await log_audit(
        db,
        actor_user_id=user.id,
        action=actions.CREDENTIAL_DELETE,
        resource_type="credential",
        resource_id=cred.id,
        metadata={"name": cred.name},
        ip_address=request.client.host if request.client else None,
    )

    await db.commit()  # 原子提交：删除 + 审计日志同时落库


# ─── 内部工具函数 ─────────────────────────────────────────────────────────────

def _to_response(cred: GitCredential) -> CredentialResponse:
    """将 GitCredential ORM 实例转换为 CredentialResponse Pydantic schema。

    用于统一 GET / POST 的返回格式，避免重复代码（DRY 原则）。

    Args:
        cred: GitCredential ORM 实例（从数据库取出的行）。

    Returns:
        CredentialResponse：安全的响应体（不含 encrypted_token）。
    """
    return CredentialResponse(
        id=cred.id,
        name=cred.name,
        type=cred.type,
        token_hint=cred.token_hint,
        # .isoformat()：datetime → ISO8601 字符串（如 '2026-05-12T08:00:00'）
        created_at=cred.created_at.isoformat(),
    )
