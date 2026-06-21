"""GitHubAppProvider：RS256 JWT 换 1h installation token（内存缓存），httpx 调 REST。
设计 §6/§7。token 不落库，缓存到接近过期重取。"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from jose import jwt

from src.service.scm.config import GitHubAppConfig

_API = "https://api.github.com"
_JWT_TTL = 540          # App JWT 9 分钟（GitHub 上限 10min）
_TOKEN_SKEW = 60        # installation token 提前 60s 视为过期


class GitHubAppProvider:
    def __init__(self, cfg: GitHubAppConfig):
        self._cfg = cfg
        self._tok_cache: dict[int, tuple[str, float]] = {}

    def _app_jwt(self) -> str:
        """用 App 私钥签 RS256 JWT（iss=app_id），用于换 installation token。"""
        now = int(time.time())
        payload = {"iat": now - 30, "exp": now + _JWT_TTL, "iss": self._cfg.app_id}
        return jwt.encode(payload, self._cfg.private_key_pem, algorithm="RS256")

    async def get_installation_token(self, installation_id: int) -> str:
        """换 1h installation token；缓存命中且未近过期则复用。"""
        cached = self._tok_cache.get(installation_id)
        if cached and cached[1] - _TOKEN_SKEW > time.time():
            return cached[0]
        headers = {
            "Authorization": f"Bearer {self._app_jwt()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_API}/app/installations/{installation_id}/access_tokens", headers=headers
            )
            resp.raise_for_status()
            data = resp.json()
        token = data["token"]
        exp = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
        self._tok_cache[installation_id] = (token, exp)
        return token
