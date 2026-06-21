"""SCM 连接 onboarding 路由（注入式工厂，便于测试）。设计 §8/§12。"""
from __future__ import annotations

import os
import secrets
from typing import Callable, Optional

from fastapi import APIRouter, Depends


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

    return router
