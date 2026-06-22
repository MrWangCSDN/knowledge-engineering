import asyncio
import httpx
import pytest, pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from src.service.db import Base
from src.service.auth_models import User
from src.service.db_models_homepage import ScmConnection
from src.service.scm.base import ScmRole
from src.service.scm.oauth_factory import OAuthProviderUnavailable
from src.service.scm.config import OAuthConfig, GitHubOAuthConfig, GitLabOidcConfig
from src.service.scm.scm_token_store import upsert_token
from src.service.scm.scm_authz import create_authorize_scm, flag_on


def _cfg():
    return OAuthConfig(redirect_base="https://ke",
                       github=GitHubOAuthConfig("gid", "gsec"),
                       gitlab=GitLabOidcConfig("https://gl", "lid", "lsec"))


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def _seed_user(maker, *, gitlab_sub=None):
    async with maker() as s:
        u = User(email="a@x.com", username="alice", hashed_password="h", gitlab_sub=gitlab_sub)
        s.add(u); await s.commit(); return u


class _FakeProvider:
    def __init__(self, *, role=None, exc=None, sleep=0.0):
        self._role, self._exc, self._sleep = role, exc, sleep
        self.kind = None
    async def resolve_repo_role(self, *, token, repo, principal):
        self.kind = "bare"
        if self._sleep:
            await asyncio.sleep(self._sleep)
        if self._exc:
            raise self._exc
        return self._role


def _factory(provider_obj=None, *, unavailable=False):
    def _get_login_provider(provider, cfg):
        if unavailable:
            raise OAuthProviderUnavailable(f"{provider} 未配置")
        return provider_obj
    return _get_login_provider


def _conn(provider="github", auth_type="github_app"):
    return ScmConnection(id="c1", provider=provider, auth_type=auth_type,
                         github_installation_id=(7 if provider == "github" else None),
                         account_login="o", status="active")


def flag(monkeypatch, name="KE_SCM_BIND_AUTHZ"):
    monkeypatch.setenv(name, "1")


@pytest.mark.asyncio
async def test_github_can_bind(maker):
    u = await _seed_user(maker)
    async with maker() as s:
        await upsert_token(s, user_id=u.id, provider="github", access_token="AT",
                           refresh_token=None, expires_at=None, scopes=None, scm_login="octocat")
        await s.commit()
    fake = _FakeProvider(role=ScmRole.CAN_BIND)
    authz = create_authorize_scm(oauth_cfg=_cfg(), get_login_provider=_factory(fake))
    async with maker() as s:
        role = await authz(s, user=u, conn=_conn(), repo_full_name="o/r", repo_external_id=42, need_bind=True)
    assert role == ScmRole.CAN_BIND and fake.kind == "bare"


@pytest.mark.asyncio
async def test_missing_token_row_403(maker):
    from fastapi import HTTPException
    u = await _seed_user(maker)   # 无 user_scm_token 行 → github principal None → 403
    authz = create_authorize_scm(oauth_cfg=_cfg(), get_login_provider=_factory(_FakeProvider(role=ScmRole.CAN_BIND)))
    async with maker() as s:
        with pytest.raises(HTTPException) as ei:
            await authz(s, user=u, conn=_conn(), repo_full_name="o/r", repo_external_id=42, need_bind=True)
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_provider_unconfigured_503(maker):
    u = await _seed_user(maker)
    async with maker() as s:
        await upsert_token(s, user_id=u.id, provider="github", access_token="AT",
                           refresh_token=None, expires_at=None, scopes=None, scm_login="octocat")
        await s.commit()
    authz = create_authorize_scm(oauth_cfg=_cfg(), get_login_provider=_factory(unavailable=True))
    from fastapi import HTTPException
    async with maker() as s:
        with pytest.raises(HTTPException) as ei:
            await authz(s, user=u, conn=_conn(), repo_full_name="o/r", repo_external_id=42, need_bind=True)
    assert ei.value.status_code == 503


@pytest.mark.asyncio
async def test_resolve_5xx_502(maker):
    u = await _seed_user(maker)
    async with maker() as s:
        await upsert_token(s, user_id=u.id, provider="github", access_token="AT",
                           refresh_token=None, expires_at=None, scopes=None, scm_login="octocat")
        await s.commit()
    req = httpx.Request("GET", "https://api.github.com/x")
    exc = httpx.HTTPStatusError("boom", request=req, response=httpx.Response(500, request=req))
    authz = create_authorize_scm(oauth_cfg=_cfg(), get_login_provider=_factory(_FakeProvider(exc=exc)))
    from fastapi import HTTPException
    async with maker() as s:
        with pytest.raises(HTTPException) as ei:
            await authz(s, user=u, conn=_conn(), repo_full_name="o/r", repo_external_id=42, need_bind=True)
    assert ei.value.status_code == 502


@pytest.mark.asyncio
async def test_resolve_connecterror_502(maker):
    u = await _seed_user(maker)
    async with maker() as s:
        await upsert_token(s, user_id=u.id, provider="github", access_token="AT",
                           refresh_token=None, expires_at=None, scopes=None, scm_login="octocat")
        await s.commit()
    authz = create_authorize_scm(oauth_cfg=_cfg(),
        get_login_provider=_factory(_FakeProvider(exc=httpx.ConnectError("no route"))))
    from fastapi import HTTPException
    async with maker() as s:
        with pytest.raises(HTTPException) as ei:
            await authz(s, user=u, conn=_conn(), repo_full_name="o/r", repo_external_id=42, need_bind=True)
    assert ei.value.status_code == 502


@pytest.mark.asyncio
async def test_timeout_502(maker):
    u = await _seed_user(maker)
    async with maker() as s:
        await upsert_token(s, user_id=u.id, provider="github", access_token="AT",
                           refresh_token=None, expires_at=None, scopes=None, scm_login="octocat")
        await s.commit()
    authz = create_authorize_scm(oauth_cfg=_cfg(),
        get_login_provider=_factory(_FakeProvider(role=ScmRole.CAN_BIND, sleep=0.2)), scm_timeout=0.01)
    from fastapi import HTTPException
    async with maker() as s:
        with pytest.raises(HTTPException) as ei:
            await authz(s, user=u, conn=_conn(), repo_full_name="o/r", repo_external_id=42, need_bind=True)
    assert ei.value.status_code == 502


@pytest.mark.asyncio
async def test_gitlab_principal_is_sub(maker):
    u = await _seed_user(maker, gitlab_sub="7")
    captured = {}
    class _GL:
        async def resolve_repo_role(self, *, token, repo, principal):
            captured["principal"] = principal; captured["repo"] = repo
            return ScmRole.CAN_QUERY
    async with maker() as s:
        await upsert_token(s, user_id=u.id, provider="gitlab", access_token="AT",
                           refresh_token=None, expires_at=None, scopes=None, scm_login="glname")
        await s.commit()
    from src.service.scm import scm_perm_cache
    scm_perm_cache.cache_clear()
    authz = create_authorize_scm(oauth_cfg=_cfg(), get_login_provider=_factory(_GL()))
    async with maker() as s:
        role = await authz(s, user=u, conn=_conn(provider="gitlab", auth_type="github_app"),
                           repo_full_name="o/r", repo_external_id=42, need_bind=False)
    assert role == ScmRole.CAN_QUERY
    assert captured["principal"] == "7" and captured["repo"] == 42   # gitlab principal=sub, repo=external_id
    # I6：need_bind=False 走 cached → CAN_QUERY 入缓存（证明走的是缓存路径而非裸调）
    assert any(k[1] == "c1" for k in scm_perm_cache._CACHE)
    scm_perm_cache.cache_clear()


def test_flag_on(monkeypatch):
    monkeypatch.delenv("KE_X", raising=False)
    assert flag_on("KE_X") is False
    monkeypatch.setenv("KE_X", "TRUE")
    assert flag_on("KE_X") is True
    monkeypatch.setenv("KE_X", "0")
    assert flag_on("KE_X") is False
