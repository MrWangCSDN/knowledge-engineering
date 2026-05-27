"""仓库管理 v1.0 — admin only 路由。

设计文档：[[仓库管理-设计]]

路由：
  GET    /admin/credentials                凭证审计（列全部；仅 hint，不含 token 原文）
  DELETE /admin/credentials/{id}           强删凭证（带审计日志）
  注意：POST /admin/credentials 已移到 /credentials（用户级，v2.0）

  POST   /admin/projects/test-connection   git ls-remote 测试连接
  POST   /admin/projects                   创建工程含 git 配置
  GET    /admin/projects                   admin 视角列表
  PATCH  /admin/projects/{id}              部分更新
  DELETE /admin/projects/{id}              删除

所有路由要求 user.is_admin == True。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.service import git_utils, token_crypto
from src.service.admin_models import (
    AdminProjectCreateRequest,
    AdminProjectListResponse,
    AdminProjectResponse,
    AdminProjectUpdateRequest,
    CredentialListResponse,
    CredentialResponse,
    TestConnectionRequest,
    TestConnectionResponse,
)
from src.service.audit.logger import log_audit  # 审计日志写入（不 commit，与业务事务一起提交）
from src.service.audit import actions            # 审计动作常量
from src.service.auth_dependencies import get_current_user
from src.service.auth_models import User
from src.service.db import get_db
from src.service.db_models_homepage import (
    GitCredential,
    Project as ProjectModel,
)
from src.service.deps_infra import require_infra_healthy  # 设计 §3.3：基础设施不可用时返 503


router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    # 设计 §3.3：任一 critical 依赖挂 → 503 INFRA_UNHEALTHY
    dependencies=[Depends(require_infra_healthy)],
)


# ─── 工具：admin 守卫 ───────────────────────────────────────────────────

async def require_admin(user: User = Depends(get_current_user)) -> User:
    """所有 /admin/* 路由的依赖。非 admin → 403。"""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    return user


# ─── 工具：DateTime → ISO ────────────────────────────────────────────

def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    fixed = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    return fixed.isoformat().replace("+00:00", "Z")


def _credential_to_pydantic(c: GitCredential) -> CredentialResponse:
    return CredentialResponse(
        id=c.id,
        name=c.name,
        type=c.type,
        token_hint=c.token_hint,
        created_by=c.created_by,
        created_at=_iso(c.created_at) or "",
        last_used_at=_iso(c.last_used_at),
    )


def _project_to_admin_pydantic(p: ProjectModel) -> AdminProjectResponse:
    raw = p.indexing_progress or {}
    return AdminProjectResponse(
        id=p.id,
        name=p.name,
        domain=raw.get("domain"),  # v1.0 still stored in JSON; W7 加正式字段
        status=p.status,
        git_url=p.git_url,
        git_branch=p.git_branch,
        credential_id=p.git_credential_id,
        last_synced_at=_iso(p.last_synced_at),
        last_synced_commit=p.last_synced_commit,
        sync_schedule=p.sync_schedule,
        created_at=_iso(p.created_at) or "",
        created_by=p.created_by,
    )


# ─── 凭证审计（admin 视角）────────────────────────────────────────────────────
# v2.0 变更：
#   - 移除 POST /admin/credentials（创建凭证已移到 /credentials，用户级操作）
#   - 保留 GET  /admin/credentials（审计：列所有用户的凭证，仅 hint，不含原文）
#   - 保留 DELETE /admin/credentials/{id}（强删：可删任何人的凭证，带审计日志）

@router.get("/credentials", response_model=CredentialListResponse)
async def list_credentials(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin),  # 下划线前缀：表示参数仅用于触发守卫，不在函数体中使用
) -> CredentialListResponse:
    """列出所有凭证（审计视角）。

    Instance Admin 专用：可看到全部用户的凭证列表。
    出于安全，响应体中永远不含 encrypted_token / token 原文，仅含末 4 位 hint。

    Args:
        db: 当前请求的异步数据库 session。
        _user: require_admin 守卫注入，确认调用方是 admin（不直接使用）。

    Returns:
        CredentialListResponse：所有凭证的元数据列表。
    """
    # select(GitCredential)：查 git_credentials 全表
    # .order_by(...)：按创建时间倒序（最新的排最前）
    result = await db.execute(
        select(GitCredential).order_by(GitCredential.created_at.desc())
    )
    return CredentialListResponse(
        credentials=[_credential_to_pydantic(c) for c in result.scalars()]
    )


@router.delete(
    "/credentials/{credential_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_credential(
    credential_id: str,
    request: Request,                          # 获取客户端 IP，写审计日志
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),       # 改为 user（非下划线），因为需要 user.id 写审计
) -> None:
    """强删凭证（admin 操作，带审计日志）。

    可删除任意用户的凭证（不受 owner_user_id 限制）。
    projects.git_credential_id 设了 ON DELETE SET NULL，
    所以关联工程会变成"没凭证"状态，不级联删工程。

    Args:
        credential_id: 路径参数，目标凭证 ID。
        request: FastAPI Request，用于提取客户端 IP 写审计日志。
        db: 当前请求的异步数据库 session。
        user: require_admin 守卫注入的 admin 用户（用于写审计 actor_user_id）。
    """
    cred = await db.get(GitCredential, credential_id)
    if cred is None:
        raise HTTPException(status_code=404, detail="凭证不存在")

    await db.delete(cred)  # 标记删除（等 commit）

    # 写审计日志：admin 强删，附加 admin_action=True 和原始 owner_id，方便事后溯源
    await log_audit(
        db,
        actor_user_id=user.id,                    # admin 操作人 ID
        action=actions.CREDENTIAL_DELETE,          # 动作："credential.delete"
        resource_type="credential",
        resource_id=cred.id,
        metadata={
            "name": cred.name,
            "admin_action": True,                 # 标记这是 admin 强删，区别于用户自删
            "original_owner_id": cred.owner_user_id,  # 记录原始归属用户 ID，方便审计追溯
        },
        ip_address=request.client.host if request.client else None,
    )

    await db.commit()  # 原子提交：删除 + 审计日志同时落库


# ─── 测试连接 ──────────────────────────────────────────────────────────

@router.post("/projects/test-connection", response_model=TestConnectionResponse)
async def test_connection(
    body: TestConnectionRequest,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin),
) -> TestConnectionResponse:
    """git ls-remote 测试 URL+凭证有效性。不写库（含 last_used_at）。"""
    plain_token: Optional[str] = None
    if body.credential_id:
        cred = await db.get(GitCredential, body.credential_id)
        if cred is None:
            raise HTTPException(status_code=404, detail="凭证不存在")
        try:
            plain_token = token_crypto.decrypt_token(cred.encrypted_token)
        except ValueError:
            return TestConnectionResponse(
                ok=False, error="凭证解密失败（密钥可能轮换或密文损坏）"
            )

    result = await git_utils.ls_remote(body.git_url, token=plain_token)
    return TestConnectionResponse(
        ok=result.ok,
        default_branch=result.default_branch,
        last_commit=result.last_commit,
        error=result.error,
    )


# ─── admin project CRUD ──────────────────────────────────────────────

@router.get("/projects", response_model=AdminProjectListResponse)
async def list_admin_projects(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin),
) -> AdminProjectListResponse:
    """admin 视角的工程列表（含 git 配置字段）。"""
    result = await db.execute(
        select(ProjectModel).order_by(ProjectModel.created_at.desc())
    )
    return AdminProjectListResponse(
        projects=[_project_to_admin_pydantic(p) for p in result.scalars()]
    )


@router.post(
    "/projects",
    response_model=AdminProjectResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_admin_project(
    body: AdminProjectCreateRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> AdminProjectResponse:
    """创建工程（含 git 配置）。

    v1.0 不实际 clone 也不跑 pipeline；仅保存元信息。
    status 默认 'configured'，等 v1.1 接通 pipeline 后会切到 indexing→ready。
    """
    if await db.get(ProjectModel, body.id):
        raise HTTPException(status_code=409, detail=f"工程 ID 已存在: {body.id}")

    if body.credential_id:
        cred = await db.get(GitCredential, body.credential_id)
        if cred is None:
            raise HTTPException(status_code=404, detail="凭证不存在")

    p = ProjectModel(
        id=body.id,
        name=body.name,
        language=body.language,
        status="configured",
        git_url=body.git_url,
        git_branch=body.git_branch,
        git_credential_id=body.credential_id,
        # v1.0: domain 暂存到 indexing_progress JSON 里；W7 加独立字段
        indexing_progress={"domain": body.domain} if body.domain else None,
        created_by=user.username,
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return _project_to_admin_pydantic(p)


@router.patch("/projects/{project_id}", response_model=AdminProjectResponse)
async def update_admin_project(
    project_id: str,
    body: AdminProjectUpdateRequest,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin),
) -> AdminProjectResponse:
    """部分更新工程字段。只更新非 None 的字段。"""
    p = await db.get(ProjectModel, project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="工程不存在")

    if body.credential_id is not None:
        # 校验新 credential 存在（空字符串等同于解除关联）
        if body.credential_id and not await db.get(GitCredential, body.credential_id):
            raise HTTPException(status_code=404, detail="凭证不存在")
        p.git_credential_id = body.credential_id or None

    if body.name is not None:
        p.name = body.name
    if body.git_url is not None:
        p.git_url = body.git_url
    if body.git_branch is not None:
        p.git_branch = body.git_branch
    if body.domain is not None:
        # 把 domain 合并到 indexing_progress JSON
        progress = dict(p.indexing_progress or {})
        progress["domain"] = body.domain
        p.indexing_progress = progress

    await db.commit()
    await db.refresh(p)
    return _project_to_admin_pydantic(p)


@router.delete(
    "/projects/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_admin_project(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin),
) -> None:
    """删除工程（含级联：sessions / messages / feedback）。

    git_credentials 不级联删（FK ON DELETE SET NULL，凭证可复用给其他工程）。
    """
    p = await db.get(ProjectModel, project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="工程不存在")
    await db.delete(p)
    await db.commit()
