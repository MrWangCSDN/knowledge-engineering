"""密码 hash/verify + JWT 编解码 + cookie 配置工具。

env vars:
  KE_JWT_SECRET            必填，≥ 32 字节
  KE_JWT_ALGORITHM         默认 HS256
  KE_JWT_ACCESS_TTL_MIN    默认 60 (分钟)
  KE_JWT_REFRESH_TTL_DAYS  默认 1 (天)
  KE_JWT_REFRESH_TTL_REMEMBER_DAYS  默认 7 (天)
  KE_COOKIE_DOMAIN / KE_COOKIE_SECURE / KE_COOKIE_SAMESITE
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import JWTError, jwt
from passlib.context import CryptContext


# bcrypt cost factor 12（设计文档约定）
_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)


def hash_password(plain: str) -> str:
    """生成 bcrypt 哈希（约 60 字符固定长度）。"""
    return _pwd_ctx.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """常量时间对比，防 timing attack。"""
    try:
        return _pwd_ctx.verify(plain, hashed)
    except Exception:
        return False


def _jwt_secret() -> str:
    s = os.getenv("KE_JWT_SECRET", "")
    if not s or len(s) < 32:
        raise RuntimeError("KE_JWT_SECRET 必须 ≥ 32 字符；用 `openssl rand -hex 32` 生成")
    return s


def _jwt_alg() -> str:
    return os.getenv("KE_JWT_ALGORITHM", "HS256")


def access_ttl_seconds() -> int:
    return int(os.getenv("KE_JWT_ACCESS_TTL_MIN", "60")) * 60


def refresh_ttl_seconds(remember_me: bool) -> int:
    days = int(
        os.getenv("KE_JWT_REFRESH_TTL_REMEMBER_DAYS", "7")
        if remember_me
        else os.getenv("KE_JWT_REFRESH_TTL_DAYS", "1")
    )
    return days * 86400


def create_access_token(*, user_id: int, username: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),       # JWT 标准：subject
        "username": username,
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=access_ttl_seconds())).timestamp()),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=_jwt_alg())


def create_refresh_token(*, user_id: int, remember_me: bool) -> str:
    now = datetime.now(timezone.utc)
    # jti 让每个 refresh token 唯一（未来加 token revocation 时按 jti 拉黑）
    payload = {
        "sub": str(user_id),
        "type": "refresh",
        "jti": secrets.token_urlsafe(16),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=refresh_ttl_seconds(remember_me))).timestamp()),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=_jwt_alg())


def decode_token(token: str) -> Optional[dict]:
    """解码 JWT；签名错或过期返回 None。"""
    try:
        return jwt.decode(token, _jwt_secret(), algorithms=[_jwt_alg()])
    except JWTError:
        return None


def cookie_settings(remember_me: bool) -> dict:
    """统一 Set-Cookie 参数。"""
    return {
        "key": "refresh_token",
        "httponly": True,
        "secure": os.getenv("KE_COOKIE_SECURE", "true").lower() == "true",
        "samesite": os.getenv("KE_COOKIE_SAMESITE", "strict").lower(),
        "domain": os.getenv("KE_COOKIE_DOMAIN") or None,
        # cookie path 跟 router 挂载路径绑定：直接 :8000/auth → "/auth"，
        # 经 nginx /api/* 反代时 → "/api/auth"
        "path": os.getenv("KE_COOKIE_PATH", "/api/auth"),
        "max_age": refresh_ttl_seconds(remember_me),
    }
