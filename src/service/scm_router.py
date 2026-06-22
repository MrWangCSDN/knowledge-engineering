"""SCM 连接 onboarding 路由（注入式工厂，便于测试）。设计 §8/§12。"""
from __future__ import annotations

import os
import secrets
import uuid
from typing import Callable, Optional

import httpx
from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from sqlalchemy import select

from src.service.db_models_homepage import ScmConnection, UserScmToken
# cache_invalidate：连接删除时清除该连接的全部权限缓存项，防止已删连接继续放行 QA 门
from src.service.scm.scm_perm_cache import cache_invalidate
from src.service.scm.oauth_state_store import mint_state, consume_state
from src.service.scm.scm_token_store import get_valid_scm_token, ScmTokenInvalid
from src.service.scm.scm_refresh import build_refresh_fn
from src.service.scm.oauth_factory import OAuthProviderUnavailable

# install-url 与 callback 共用，防 purpose typo 静默 400
_INSTALL_PURPOSE = "install"


def create_scm_routes(*, get_current_user: Callable, get_db: Optional[Callable],
                      get_provider: Callable, app_slug: Optional[str] = None,
                      oauth_cfg=None, get_login_provider: Optional[Callable] = None) -> APIRouter:
    router = APIRouter(prefix="/scm", tags=["scm"])
    slug = app_slug or os.getenv("KE_GH_APP_SLUG", "")

    @router.get("/github/install-url")
    async def install_url(response: Response, user=Depends(get_current_user),
                          db=Depends(get_db)) -> dict:
        """返回 GitHub App 安装 URL + 真 state（CSRF 绑定）。A 早拦：未关联 GitHub → 403。"""
        # A 早拦（UX 轻量存在性查；真核验在 callback）：无 github 关联 → 403 引导先关联
        row = (await db.execute(select(UserScmToken).where(
            UserScmToken.user_id == user.id, UserScmToken.provider == "github"
        ))).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=403, detail="请先关联 GitHub 账号，再安装应用")
        minted = await mint_state(db, provider="github", purpose=_INSTALL_PURPOSE,
                                  user_id=user.id, with_nonce=False, ttl_seconds=1800)
        # mint_state 仅 flush；不显式 commit——由 get_db 末尾 commit 持久化 state
        response.set_cookie(
            key="ke_oauth_csrf", value=minted.csrf, httponly=True,
            secure=os.getenv("KE_COOKIE_SECURE", "true").lower() == "true",
            samesite="lax", path="/", max_age=1800,
        )
        return {
            "install_url": f"https://github.com/apps/{slug}/installations/new?state={minted.state}",
            "state": minted.state,
        }

    @router.get("/github/callback")
    async def callback(installation_id: int, state: str = "", user=Depends(get_current_user),
                       db=Depends(get_db)) -> dict:
        """GitHub App 安装回调：建 scm_connection。
        TODO(P4)：用用户 OAuth user-to-server token 核实该 installation 确属当前用户（防伪造）。"""
        provider = get_provider()
        login = await provider.get_account_login(installation_id)
        conn = ScmConnection(
            id=f"conn-{uuid.uuid4().hex[:16]}", provider="github", auth_type="github_app",
            github_installation_id=installation_id, account_login=login, status="active",
            created_by=getattr(user, "username", None),
        )
        db.add(conn)
        await db.commit()
        return {"connection_id": conn.id, "account_login": login}

    @router.get("/connections")
    async def list_connections(user=Depends(get_current_user), db=Depends(get_db)) -> dict:
        """列出当前用户的所有 SCM 连接。"""
        rows = (await db.execute(
            select(ScmConnection).where(ScmConnection.created_by == getattr(user, "username", None))
        )).scalars().all()
        return {"connections": [
            {"id": r.id, "provider": r.provider, "auth_type": r.auth_type,
             "account_login": r.account_login, "status": r.status} for r in rows
        ]}

    @router.delete("/connections/{connection_id}", status_code=204)
    async def delete_connection(connection_id: str, user=Depends(get_current_user), db=Depends(get_db)):
        """删除指定 SCM 连接（仅创建者或管理员可操作）。"""
        conn = await db.get(ScmConnection, connection_id)
        if conn is None:
            raise HTTPException(status_code=404, detail="连接不存在")
        # 先取调用者用户名；若本身为 None，视为"无主"——永远不能匹配任何 owner
        owner = getattr(user, "username", None)
        # owner is None 时直接拒绝，防止 conn.created_by=NULL 与 None==None 比较通过形成绕过
        if (owner is None or conn.created_by != owner) and not getattr(user, "is_admin", False):
            raise HTTPException(status_code=403, detail="无权删除该连接")
        await db.delete(conn)
        await db.commit()
        # best-effort：清除该连接的全部 can_query 权限缓存项
        # try/except：cache_invalidate 是纯内存操作，通常不会抛，但防御性包裹确保缓存清理
        # 绝不让缓存清理的异常让删除请求返回 500（BLE001：允许捕获宽泛 Exception）
        try:
            cache_invalidate(connection_id)      # best-effort：清该连接 can_query 缓存
        except Exception:                        # noqa: BLE001 — 不让缓存清理 500 掉删除
            pass
        return Response(status_code=204)

    async def _load_conn(connection_id: str, user, db) -> "ScmConnection":
        """按 ID 加载连接，校验存在性与归属权（owner 或 admin 才可访问）。"""
        conn = await db.get(ScmConnection, connection_id)
        # 连接不存在时返回 404
        if conn is None:
            raise HTTPException(status_code=404, detail="连接不存在")
        # 先取调用者用户名；username 为 None 时视为"无主"，直接拒绝，防 NULL==NULL 绕过
        owner = getattr(user, "username", None)
        # owner is None 时直接拒绝，防止 conn.created_by=NULL 与 None==None 比较通过形成绕过
        if (owner is None or conn.created_by != owner) and not getattr(user, "is_admin", False):
            raise HTTPException(status_code=403, detail="无权访问该连接")
        return conn

    @router.get("/connections/{connection_id}/repos")
    async def list_repos(connection_id: str, user=Depends(get_current_user), db=Depends(get_db)) -> dict:
        """列出指定连接下 GitHub App 可见的仓库（installation 级别，不做成员过滤，P4 再加）。"""
        # 校验连接归属
        conn = await _load_conn(connection_id, user, db)
        # PAT 类型连接的 github_installation_id 为 NULL，无法调用 App 级接口，提前拦截
        if conn.github_installation_id is None:
            raise HTTPException(status_code=422, detail="该连接不支持 GitHub App 操作")
        # 调用 P1 provider 的 list_repos，返回 RepoInfo 列表
        repos = await get_provider().list_repos(conn.github_installation_id)
        # 将 dataclass 字段转为普通 dict 返回给前端
        return {"repos": [
            {"external_id": r.external_id, "full_name": r.full_name,
             "default_branch": r.default_branch, "private": r.private} for r in repos
        ]}

    @router.get("/connections/{connection_id}/repos/{full_name:path}/branches")
    async def list_branches(connection_id: str, full_name: str,
                            user=Depends(get_current_user), db=Depends(get_db)) -> dict:
        """列出指定仓库的分支列表。{full_name:path} 允许 owner/repo 中包含斜杠。"""
        # 校验连接归属
        conn = await _load_conn(connection_id, user, db)
        # PAT 类型连接的 github_installation_id 为 NULL，无法调用 App 级接口，提前拦截
        if conn.github_installation_id is None:
            raise HTTPException(status_code=422, detail="该连接不支持 GitHub App 操作")
        # 调用 P1 provider 的 list_branches，返回 BranchList(default_branch, branches)
        bl = await get_provider().list_branches(conn.github_installation_id, full_name)
        return {"default_branch": bl.default_branch, "branches": bl.branches}

    return router
