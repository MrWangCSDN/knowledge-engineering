"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class LoginRequest(BaseModel):
    """POST /auth/login body."""
    username: str = Field(..., min_length=3, max_length=255, description="邮箱或用户名")
    password: str = Field(..., min_length=8, max_length=128)
    remember_me: bool = Field(False, description="勾选后 refresh_token TTL=7 天")


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(..., description="access_token 过期秒数")


class RefreshResponse(BaseModel):
    access_token: str
    expires_in: int


class MeResponse(BaseModel):
    """GET /auth/me 返回当前用户。"""
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: EmailStr
    username: str
    is_admin: bool
    created_at: datetime


class LogoutResponse(BaseModel):
    ok: bool = True
