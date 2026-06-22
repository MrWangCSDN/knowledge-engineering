# src/service/scm_oauth_router.py
"""SCM OAuth/OIDC 登录 + 关联路由（注入式工厂）。设计 §4.4/§6。"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Callable, Literal, Optional

import src.service.auth_security as sec
from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select

from src.service.auth_models import User
from src.service.db_models_homepage import UserScmToken
from src.service.scm.oauth_factory import OAuthProviderUnavailable
from src.service.scm.oauth_state_store import mint_state, consume_state, gc_expired
from src.service.scm.scm_token_store import upsert_token, delete_token

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
        # SameSite=Lax（不能用 Strict）：Strict 会让浏览器在 IdP→callback 的跨站顶级重定向时
        # 丢弃 cookie（因为请求来源是第三方站点），导致 callback 永远读不到 csrf cookie → 400。
        # Lax 在顶级 GET 导航（正是 OAuth callback 的场景）时会携带 cookie，同时仍阻止跨站 POST/XHR。
        resp.set_cookie(key=_CSRF_COOKIE, value=csrf, httponly=True,
                        secure=os.getenv("KE_COOKIE_SECURE", "true").lower() == "true",
                        samesite="lax", path="/", max_age=600)

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
            # expires_in 存在时（GitHub App token 有有效期）计算绝对 expires_at；
            # 与 GitLab 分支保持一致，使 get_valid_scm_token 可在到期时自动刷新 token。
            # GitHub 普通 OAuth App token 不含 expires_in（None），设为 None 表示永不过期。
            exp = None
            if tok.get("expires_in"):
                # timedelta(seconds=...) 构造时间差；now(utc) + timedelta = 未来绝对时间点
                exp = datetime.now(timezone.utc) + timedelta(seconds=int(tok["expires_in"]))
            persist = {"access_token": tok["access_token"], "refresh_token": tok.get("refresh_token"),
                       "expires_at": exp, "scopes": tok.get("scope")}
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
        # --- link 分支：当前用户把 SCM 身份关联到自己账号 ---
        if st.purpose == "link":
            if st.user_id is None:
                raise HTTPException(status_code=400, detail="link state 缺 user_id")
            col = _ID_COL[provider]
            # I1：归一列类型再比较（github→int/BigInteger，gitlab→str）
            lookup_val = _id_lookup_value(provider, ident.scm_user_id)
            # 检查该 SCM id 是否已被他人占用（排除自己）
            taken = (await db.execute(
                select(User).where(getattr(User, col) == lookup_val, User.id != st.user_id)
            )).scalar_one_or_none()
            if taken is not None:
                raise HTTPException(status_code=409, detail="该 SCM 身份已被其他账号关联")
            # 加载当前用户行
            me = (await db.execute(select(User).where(User.id == st.user_id))).scalar_one_or_none()
            if me is None:
                raise HTTPException(status_code=404, detail="用户不存在")
            cur = getattr(me, col)
            # 自己已绑不同 id → 禁止静默替换，要求先解绑
            if cur is not None and str(cur) != str(ident.scm_user_id):
                raise HTTPException(status_code=409, detail="已关联其他账号，请先解绑")
            # 写身份列（setattr 直接在 ORM 对象上赋值，session.flush 时入库）
            setattr(me, col, lookup_val)
            await upsert_token(db, user_id=me.id, provider=provider,
                               access_token=persist["access_token"], refresh_token=persist["refresh_token"],
                               expires_at=persist["expires_at"], scopes=persist["scopes"],
                               scm_login=ident.login)
            # 302 跳回设置页，让前端刷新关联状态
            return RedirectResponse(f"{oauth_config.redirect_base}/settings", status_code=302)
        raise HTTPException(status_code=400, detail="未知 purpose")

    @router.post("/account/link-scm/{provider}/start")
    async def link_start(provider: str, user=Depends(get_current_user), db=Depends(get_db)):
        """已登录用户发起 SCM 关联授权流程：mint link-purpose state，返回 authorize_url。"""
        prov = _provider_or_503(provider)   # 未知→404、未配→503
        # GitLab 是 OIDC，需要额外的 nonce；GitHub 用普通 OAuth，nonce=None
        is_oidc = provider == "gitlab"
        minted = await mint_state(db, provider=provider, purpose="link", user_id=user.id,
                                  with_nonce=is_oidc)
        if provider == "github":
            # GitHub 用同步 build_authorize_url（返回字符串）
            url = prov.build_authorize_url(client_id=oauth_config.github.client_id,
                                           redirect_uri=_redirect_uri(provider), state=minted.state)
        else:
            # GitLab OIDC 用 async build_authorize_url
            url = await prov.build_authorize_url(redirect_uri=_redirect_uri(provider),
                                                 state=minted.state, nonce=minted.nonce)
        # 返回 JSON（前端弹窗处理，不像 login 那样直接 302 重定向）
        resp = JSONResponse({"authorize_url": url})
        _set_csrf(resp, minted.csrf)
        return resp

    @router.get("/account/scm-links")
    async def scm_links(user=Depends(get_current_user), db=Depends(get_db)) -> dict:
        """查询当前用户已关联的所有 SCM provider 列表。"""
        # 拉取该用户全部 token 行（每个 provider 一行）
        rows = (await db.execute(
            select(UserScmToken).where(UserScmToken.user_id == user.id)
        )).scalars().all()
        # 只返回展示字段，linked_at 序列化为 ISO 8601 字符串
        return {"links": [{"provider": r.provider, "scm_login": r.scm_login,
                           "linked_at": r.linked_at.isoformat() if r.linked_at else None} for r in rows]}

    @router.delete("/account/scm-links/{provider}", status_code=204)
    async def unlink(provider: str, user=Depends(get_current_user), db=Depends(get_db)):
        """解除当前用户对指定 provider 的 SCM 关联。幂等：已解绑也返回 204。"""
        # B1：未知 provider → 404；不走 _provider_or_503，解绑不需要 provider 客户端配置
        if provider not in _KNOWN_PROVIDERS:
            raise HTTPException(status_code=404, detail=f"未知 provider: {provider}")
        me = (await db.execute(select(User).where(User.id == user.id))).scalar_one_or_none()
        if me is not None:
            # 清空身份列（置 None 即解绑 SCM 登录入口）
            setattr(me, _ID_COL[provider], None)
        # 删除 token 行（delete_token 内部用 DELETE WHERE，行不存在时也无报错，天然幂等）
        await delete_token(db, user_id=user.id, provider=provider)
        # I6（已记录决策）：provider 侧 token 撤销本片**有意不做**——解绑只清本地身份+token。
        # KE 是 link-only over 密码账号，解绑不会困住用户；远端撤销留后续 best-effort。
        return Response(status_code=204)

    return router
