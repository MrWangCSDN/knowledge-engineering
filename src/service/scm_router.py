"""SCM 连接 onboarding 路由（注入式工厂，便于测试）。设计 §8/§12。"""
from __future__ import annotations

import os
import secrets
import uuid
from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select

from src.service.db_models_homepage import ScmConnection


def create_scm_routes(*, get_current_user: Callable, get_db: Optional[Callable],
                      get_provider: Callable, app_slug: Optional[str] = None) -> APIRouter:
    router = APIRouter(prefix="/scm", tags=["scm"])
    slug = app_slug or os.getenv("KE_GH_APP_SLUG", "")

    @router.get("/github/install-url")
    async def install_url(user=Depends(get_current_user)) -> dict:
        """返回 GitHub App 安装 URL + 防 CSRF state（前端跳转后回带）。"""
        state = secrets.token_urlsafe(24)
        return {
            "install_url": f"https://github.com/apps/{slug}/installations/new?state={state}",
            "state": state,
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
        if conn.created_by != getattr(user, "username", None) and not getattr(user, "is_admin", False):
            raise HTTPException(status_code=403, detail="无权删除该连接")
        await db.delete(conn)
        await db.commit()
        return Response(status_code=204)

    return router
