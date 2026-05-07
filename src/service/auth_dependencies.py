"""FastAPI Auth dependencies：oauth2_scheme + get_current_user。"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.service.auth_models import User
from src.service.auth_security import decode_token
from src.service.db import get_db


# tokenUrl 仅用于 swagger UI 的 "Try it out" 按钮，实际请求走 /auth/login
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=False)


async def get_current_user(
    token: Optional[str] = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """从 Bearer token 解析当前 user；失败一律 401。"""
    cred_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token:
        raise cred_exc

    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
        raise cred_exc

    user_id_str = payload.get("sub")
    if not user_id_str:
        raise cred_exc
    try:
        user_id = int(user_id_str)
    except ValueError:
        raise cred_exc

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None or not user.is_active:
        raise cred_exc

    return user


async def get_current_admin(
    user: User = Depends(get_current_user),
) -> User:
    """需要 admin 角色（用于创建用户/重置密码等管理接口）。"""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    return user
