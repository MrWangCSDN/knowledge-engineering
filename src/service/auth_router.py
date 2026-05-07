"""4 个 /auth/* 路由。

设计文档：[[登录与认证-设计]] §3
速率限制 + 账号锁定使用最简实现（基于 users.failed_attempts 字段）。
未来如需更精细可换 redis-based。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.service import auth_security as sec
from src.service.auth_dependencies import get_current_user
from src.service.auth_models import User
from src.service.auth_schemas import (
    LoginRequest, LoginResponse,
    LogoutResponse, MeResponse, RefreshResponse,
)
from src.service.db import get_db

router = APIRouter(prefix="/auth", tags=["auth"])


_LOCK_DURATION = timedelta(minutes=15)
_MAX_FAILED = 5


@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> LoginResponse:
    """密码登录 → 签 access_token + 设 refresh_token cookie。"""
    # 按 username 或 email 查
    stmt = select(User).where(or_(User.username == body.username, User.email == body.username))
    user = (await db.execute(stmt)).scalar_one_or_none()

    # ⚠️ 不区分"用户不存在" vs "密码错"，防 user enumeration
    invalid = HTTPException(status_code=401, detail="用户名或密码不正确")

    if user is None:
        raise invalid

    # 锁定中？
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if user.locked_until and user.locked_until > now:
        remain_min = int((user.locked_until - now).total_seconds() // 60) + 1
        raise HTTPException(status_code=423, detail=f"账号已锁定，请 {remain_min} 分钟后重试")

    # is_active=False 同等"不存在"
    if not user.is_active:
        raise invalid

    if not sec.verify_password(body.password, user.hashed_password):
        user.failed_attempts += 1
        if user.failed_attempts >= _MAX_FAILED:
            user.locked_until = now + _LOCK_DURATION
        # 失败计数必须在抛异常前 commit，否则异常触发 rollback，计数丢失
        await db.commit()
        raise invalid

    # 成功：清失败计数
    user.failed_attempts = 0
    user.locked_until = None
    await db.flush()

    access = sec.create_access_token(user_id=user.id, username=user.username)
    refresh = sec.create_refresh_token(user_id=user.id, remember_me=body.remember_me)

    response.set_cookie(value=refresh, **sec.cookie_settings(body.remember_me))
    return LoginResponse(access_token=access, expires_in=sec.access_ttl_seconds())


@router.post("/refresh", response_model=RefreshResponse)
async def refresh(
    request: Request,
    refresh_token: Optional[str] = Cookie(default=None),
    db: AsyncSession = Depends(get_db),
) -> RefreshResponse:
    """用 cookie 中的 refresh_token 换新 access_token。CSRF 防御：校验 Origin。"""
    # CSRF 防御：refresh 端点要求 Origin 为同站
    origin = request.headers.get("origin", "")
    host = request.headers.get("host", "")
    if origin and host and host not in origin:
        raise HTTPException(status_code=403, detail="Cross-origin refresh forbidden")

    if not refresh_token:
        raise HTTPException(status_code=401, detail="Missing refresh cookie")

    payload = sec.decode_token(refresh_token)
    if not payload or payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    try:
        user_id = int(payload["sub"])
    except (KeyError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid token payload")

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")

    new_access = sec.create_access_token(user_id=user.id, username=user.username)
    return RefreshResponse(access_token=new_access, expires_in=sec.access_ttl_seconds())


@router.get("/me", response_model=MeResponse)
async def me(user: User = Depends(get_current_user)) -> MeResponse:
    return MeResponse.model_validate(user)


@router.post("/logout", response_model=LogoutResponse)
async def logout(response: Response) -> LogoutResponse:
    """清 refresh cookie；前端同时清内存 access_token。"""
    # path 必须与 cookie_settings() 中保持一致
    cookie_path = sec.cookie_settings(remember_me=False)["path"]
    response.delete_cookie(
        key="refresh_token",
        path=cookie_path,
        samesite="strict",
    )
    return LogoutResponse(ok=True)
