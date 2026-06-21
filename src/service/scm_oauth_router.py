# src/service/scm_oauth_router.py
"""SCM OAuth/OIDC 登录 + 关联路由（注入式工厂）。设计 §4.4/§6。"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Callable, Literal, Optional

import src.service.auth_security as sec
from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from src.service.auth_models import User
from src.service.scm.oauth_factory import OAuthProviderUnavailable
from src.service.scm.oauth_state_store import mint_state, consume_state, gc_expired
from src.service.scm.scm_token_store import upsert_token

_log = logging.getLogger("ke.scm.oauth")
_CSRF_COOKIE = "ke_oauth_csrf"
_KNOWN_PROVIDERS = {"github", "gitlab"}
Provider = Literal["github", "gitlab"]   # 仅文档别名；路由参数用 str（见 B1）
# users 列名映射
_ID_COL = {"github": "github_user_id", "gitlab": "gitlab_sub"}


def _id_lookup_value(provider: str, scm_user_id: str):
    """把 ScmIdentity.scm_user_id（字符串）归一成列类型：github→int(BigInteger)，gitlab→str。"""
    return int(scm_user_id) if provider == "github" else scm_user_id


def create_scm_oauth_routes(*, get_current_user: Callable, get_db: Callable,
                            get_login_provider: Callable, oauth_config) -> APIRouter:
    router = APIRouter(tags=["scm-oauth"])

    def _provider_or_503(provider: str):
        # B1：未知 provider → 404（须先于 503 判定；Literal/Enum 路径参数会给 422，故用 str + 显式校验）
        if provider not in _KNOWN_PROVIDERS:
            raise HTTPException(status_code=404, detail=f"未知 provider: {provider}")
        if oauth_config.provider(provider) is None:
            raise HTTPException(status_code=503, detail=f"{provider} 未配置")
        try:
            return get_login_provider(provider, oauth_config)
        except OAuthProviderUnavailable:
            raise HTTPException(status_code=503, detail=f"{provider} 未配置")

    def _redirect_uri(provider: str) -> str:
        # B5：redirect_uri 仅由服务端 redirect_base 派生，绝不取请求 Host/转发头
        return f"{oauth_config.redirect_base}/auth/{provider}/callback"

    def _set_csrf(resp: Response, csrf: str) -> None:
        resp.set_cookie(key=_CSRF_COOKIE, value=csrf, httponly=True,
                        secure=os.getenv("KE_COOKIE_SECURE", "true").lower() == "true",
                        samesite="strict", path="/", max_age=600)

    @router.get("/auth/{provider}/login")
    async def login(provider: str, db=Depends(get_db)):
        prov = _provider_or_503(provider)
        is_oidc = provider == "gitlab"
        minted = await mint_state(db, provider=provider, purpose="login", user_id=None,
                                  with_nonce=is_oidc)
        if provider == "github":
            url = prov.build_authorize_url(
                client_id=oauth_config.github.client_id,
                redirect_uri=_redirect_uri(provider), state=minted.state)
        else:
            url = await prov.build_authorize_url(
                redirect_uri=_redirect_uri(provider), state=minted.state, nonce=minted.nonce)
        resp = RedirectResponse(url, status_code=302)
        _set_csrf(resp, minted.csrf)
        return resp

    async def _resolve_identity(provider: str, prov, code: str, nonce: Optional[str]):
        """换 token + 解析身份（只来自本次 token）。返回 (identity, token_persist)。
        B4：所有上游异常在 callback 里转为通用错误，绝不把 IdP 响应体/token/code 透传或入日志。"""
        if provider == "github":
            tok = await prov.exchange_code(
                client_id=oauth_config.github.client_id,
                client_secret=oauth_config.github.client_secret,
                code=code, redirect_uri=_redirect_uri(provider))
            ident = await prov.get_login_identity({"access_token": tok["access_token"]})
            persist = {"access_token": tok["access_token"], "refresh_token": tok.get("refresh_token"),
                       "expires_at": None, "scopes": tok.get("scope")}
        else:
            tok = await prov.exchange_code(code=code, redirect_uri=_redirect_uri(provider))
            claims = await prov.validate_id_token(tok["id_token"], expected_nonce=nonce)
            ident = await prov.get_login_identity({"id_token_claims": claims})
            exp = None
            if tok.get("expires_in"):
                exp = datetime.now(timezone.utc) + timedelta(seconds=int(tok["expires_in"]))
            persist = {"access_token": tok["access_token"], "refresh_token": tok.get("refresh_token"),
                       "expires_at": exp, "scopes": tok.get("scope")}
        return ident, persist

    @router.get("/auth/{provider}/callback")
    async def callback(provider: str, state: str, code: Optional[str] = None,
                       error: Optional[str] = None, db=Depends(get_db),
                       ke_oauth_csrf: Optional[str] = Cookie(default=None)):
        prov = _provider_or_503(provider)
        # 1) 原子消费 state（先于一切写）；失败/重放/过期/csrf 不符 → 400
        st = await consume_state(db, state=state, csrf=ke_oauth_csrf)
        if st is None:
            raise HTTPException(status_code=400, detail="state 校验失败")
        await gc_expired(db)                       # I2：每次回调机会式清过期 state
        # M6：用户拒授权 / 缺 code → 通用 400（不回显 IdP error 细节，B4）
        if code is None:
            _log.info("oauth callback without code (provider=%s, purpose=%s)", provider, st.purpose)
            raise HTTPException(status_code=400, detail="授权未完成")
        # 2) 换 token + 解析身份；B4：上游异常一律转通用错误，不泄 token/code/IdP 体
        try:
            ident, persist = await _resolve_identity(provider, prov, code, st.nonce)
        except HTTPException:
            raise
        except Exception:                          # noqa: BLE001 — 统一兜成通用错误
            _log.warning("oauth token exchange/identity failed (provider=%s)", provider)
            raise HTTPException(status_code=502, detail="SCM 授权失败")

        if st.purpose == "login":
            col = _ID_COL[provider]
            user = (await db.execute(
                select(User).where(getattr(User, col) == _id_lookup_value(provider, ident.scm_user_id))
            )).scalar_one_or_none()
            if user is None:
                raise HTTPException(status_code=403,
                    detail="该 SCM 身份未关联任何 KE 账号，请先登录并在设置里关联")
            if not user.is_active:
                raise HTTPException(status_code=403, detail="账号已停用")
            lu = user.locked_until
            if lu is not None:
                if lu.tzinfo is None:
                    lu = lu.replace(tzinfo=timezone.utc)
                if lu > datetime.now(timezone.utc):
                    raise HTTPException(status_code=423, detail="账号已锁定")
            # B5 fail-closed：先写 token（加密失败→抛→get_db 不 commit→整事务回滚，
            # 不下发 cookie、不签 access），再签 JWT、再建带 cookie 的响应。顺序即保证。
            # 捕获加密/存储异常并转为 HTTPException(500) 使 FastAPI 正常返回 5xx 响应
            # （不捕获则 TestClient 默认 raise_server_exceptions=True 会把 500 抛成 Python 异常）
            try:
                await upsert_token(db, user_id=user.id, provider=provider,
                                   access_token=persist["access_token"], refresh_token=persist["refresh_token"],
                                   expires_at=persist["expires_at"], scopes=persist["scopes"],
                                   scm_login=ident.login)
            except HTTPException:
                raise
            except Exception:                      # noqa: BLE001 — 加密/IO 失败，fail-closed
                _log.error("token 持久化失败，拒绝下发凭证 (provider=%s, user_id=%s)",
                           provider, user.id)
                raise HTTPException(status_code=500, detail="内部错误，请重试")
            refresh = sec.create_refresh_token(user_id=user.id, remember_me=False)
            resp = RedirectResponse(f"{oauth_config.redirect_base}/", status_code=302)
            resp.set_cookie(value=refresh, **sec.cookie_settings(False))  # access 不进 URL，前端走 /auth/refresh
            return resp
        # link 分支 → Task 10 实现
        raise HTTPException(status_code=400, detail="link 分支未实现（Task 10）")

    return router
