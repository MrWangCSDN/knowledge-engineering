# src/service/scm/scm_authz.py
"""SCM 门核心：装配 provider→token→resolve，infra 错抛 HTTPException、返回 ScmRole。设计 §4.1。"""
from __future__ import annotations

import asyncio
import os
import httpx
from typing import Callable, Optional

from fastapi import HTTPException
from sqlalchemy import select

from src.service.scm.base import ScmRole
from src.service.scm.oauth_factory import OAuthProviderUnavailable
from src.service.scm.scm_refresh import build_refresh_fn
from src.service.scm.scm_token_store import get_valid_scm_token, ScmTokenInvalid
from src.service.db_models_homepage import UserScmToken
from src.service.scm.scm_perm_cache import resolve_repo_role_cached


def flag_on(env_name: str) -> bool:
    """per-request 读 kill-switch（沿用 KE_QA_USE_REACT 范式，额外 case-fold）。两路由共用。"""
    return os.environ.get(env_name, "").strip().lower() in {"1", "true", "yes"}


def create_authorize_scm(*, oauth_cfg, get_login_provider: Callable, scm_timeout: float = 8.0):
    async def authorize_scm(db, *, user, conn, repo_full_name, repo_external_id,
                            need_bind: bool) -> ScmRole:
        """解析 user 在 conn 所指仓的 ScmRole；infra 错抛 HTTPException，成功返 ScmRole（含 NOT_VISIBLE，路由判档）。
        前提：调用方已判 kill-switch on、conn.auth_type != 'pat'、工程已绑定。"""
        provider_name = conn.provider
        try:
            provider_obj = get_login_provider(provider_name, oauth_cfg)
        except OAuthProviderUnavailable:
            raise HTTPException(status_code=503, detail=f"{provider_name} 未配置")
        if provider_name == "github":
            row = (await db.execute(select(UserScmToken).where(
                UserScmToken.user_id == user.id, UserScmToken.provider == "github"))).scalar_one_or_none()
            principal = row.scm_login if row else None
            repo_arg = repo_full_name
        else:
            principal = str(user.gitlab_sub) if getattr(user, "gitlab_sub", None) is not None else None
            repo_arg = repo_external_id
        if not principal:
            raise HTTPException(status_code=403, detail="请先关联对应 SCM 账号")
        refresh_fn = build_refresh_fn(
            provider_name,
            gitlab_provider=provider_obj if provider_name == "gitlab" else None,
            oauth_cfg=oauth_cfg)
        try:
            token = await get_valid_scm_token(db, user_id=user.id, provider=provider_name, refresh_fn=refresh_fn)
        except ScmTokenInvalid:
            raise HTTPException(status_code=403, detail="SCM 授权已失效，请重新关联")
        except (httpx.HTTPError, RuntimeError):
            raise HTTPException(status_code=502, detail="SCM 授权刷新失败，请重试")
        try:
            if need_bind:
                role = await asyncio.wait_for(
                    provider_obj.resolve_repo_role(token=token, repo=repo_arg, principal=principal), scm_timeout)
            else:
                role = await asyncio.wait_for(
                    resolve_repo_role_cached(provider_obj, user_id=user.id, connection_id=conn.id,
                        repo_external_id=repo_external_id, token=token, repo=repo_arg, principal=principal),
                    scm_timeout)
        except (asyncio.TimeoutError, httpx.HTTPError):
            raise HTTPException(status_code=502, detail="SCM 权限校验失败，请重试")
        return role
    return authorize_scm


async def resolve_caller_token(db, *, user, provider, oauth_cfg, get_login_provider):
    """造 provider + 取调用者明文 user-token。异常→HTTPException(503/403/502)。"""
    try:
        prov = get_login_provider(provider, oauth_cfg)
    except OAuthProviderUnavailable:
        raise HTTPException(status_code=503, detail=f"{provider} 未配置")
    refresh_fn = build_refresh_fn(
        provider,
        gitlab_provider=prov if provider == "gitlab" else None,
        oauth_cfg=oauth_cfg)
    try:
        token = await get_valid_scm_token(db, user_id=user.id, provider=provider, refresh_fn=refresh_fn)
    except ScmTokenInvalid:
        raise HTTPException(status_code=403, detail="请先关联 SCM 账号")
    except (httpx.HTTPError, RuntimeError):
        raise HTTPException(status_code=502, detail="SCM 授权刷新失败，请重试")
    return prov, token
