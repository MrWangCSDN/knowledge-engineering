"""SCM 连接 onboarding 路由（注入式工厂，便于测试）。设计 §8/§12。"""
from __future__ import annotations

import os
import secrets
import uuid
from typing import Callable, Optional

from fastapi import APIRouter, Depends

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

    return router
