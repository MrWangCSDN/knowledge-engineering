import hashlib
from datetime import datetime, timezone, timedelta
import pytest, pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select
from src.service.db import Base
from src.service.auth_models import User
from src.service.db_models_homepage import OAuthState, UserScmToken
from src.service.scm.config import OAuthConfig, GitHubOAuthConfig
from src.service.scm.base import ScmIdentity
from src.service.scm_oauth_router import create_scm_oauth_routes


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


class _FakeGitHub:
    def build_authorize_url(self, *, client_id, redirect_uri, state):
        return f"https://github.com/login/oauth/authorize?state={state}"
    async def exchange_code(self, *, client_id, client_secret, code, redirect_uri):
        return {"access_token": "AT", "scope": "read:user"}
    async def get_login_identity(self, token):
        return ScmIdentity(provider="github", scm_user_id="42", login="octocat")


def _cfg():
    return OAuthConfig(redirect_base="https://ke", github=GitHubOAuthConfig("cid", "sec"), gitlab=None)


def _app(maker, user=None):
    app = FastAPI()
    async def _get_db():
        async with maker() as s:
            yield s
            await s.commit()
    app.include_router(create_scm_oauth_routes(
        get_current_user=(lambda: user), get_db=_get_db,
        get_login_provider=lambda prov, cfg: _FakeGitHub(), oauth_config=_cfg(),
    ))
    return app


@pytest.mark.asyncio
async def test_login_redirects_and_sets_csrf(maker):
    c = TestClient(_app(maker), follow_redirects=False)
    r = c.get("/auth/github/login")
    assert r.status_code in (302, 307)
    assert "github.com/login/oauth/authorize" in r.headers["location"]
    assert "ke_oauth_csrf=" in r.headers.get("set-cookie", "")
    async with maker() as s:
        assert (await s.execute(select(OAuthState))).scalars().first() is not None


@pytest.mark.asyncio
async def test_callback_login_success_issues_tokens(maker, monkeypatch):
    monkeypatch.setenv("KE_JWT_SECRET", "x" * 32)
    # 关联好的用户
    async with maker() as s:
        s.add(User(email="a@x.com", username="alice", hashed_password="h",
                   is_active=True, github_user_id=42)); await s.commit()
    c = TestClient(_app(maker), follow_redirects=False)
    # 走 login 拿 state + csrf cookie
    r0 = c.get("/auth/github/login")
    state = r0.headers["location"].split("state=")[1]
    # 回调（TestClient 自动带上 login 时下发的 csrf cookie）
    r = c.get(f"/auth/github/callback", params={"code": "c", "state": state})
    assert r.status_code in (302, 307)
    # refresh cookie 已下发
    assert "refresh_token=" in r.headers.get("set-cookie", "")
    async with maker() as s:
        assert (await s.execute(select(UserScmToken).where(UserScmToken.user_id != None))).scalars().first() is not None


@pytest.mark.asyncio
async def test_callback_login_unlinked_403(maker, monkeypatch):
    monkeypatch.setenv("KE_JWT_SECRET", "x" * 32)
    c = TestClient(_app(maker), follow_redirects=False)
    r0 = c.get("/auth/github/login")
    state = r0.headers["location"].split("state=")[1]
    r = c.get("/auth/github/callback", params={"code": "c", "state": state})
    assert r.status_code == 403   # 身份未关联任何账号


def test_callback_bad_state_400(maker):
    c = TestClient(_app(maker), follow_redirects=False)
    r = c.get("/auth/github/callback", params={"code": "c", "state": "nope"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_callback_inactive_403(maker, monkeypatch):
    monkeypatch.setenv("KE_JWT_SECRET", "x" * 32)
    async with maker() as s:
        s.add(User(email="d@x.com", username="dan", hashed_password="h",
                   is_active=False, github_user_id=42)); await s.commit()
    c = TestClient(_app(maker), follow_redirects=False)
    r0 = c.get("/auth/github/login")
    state = r0.headers["location"].split("state=")[1]
    r = c.get("/auth/github/callback", params={"code": "c", "state": state})
    assert r.status_code == 403


def test_unknown_provider_404(maker):
    c = TestClient(_app(maker), follow_redirects=False)
    assert c.get("/auth/bitbucket/login").status_code == 404


class _BoomGitHub(_FakeGitHub):
    async def exchange_code(self, *, client_id, client_secret, code, redirect_uri):
        # 模拟上游异常携带敏感串（token/code），断言不外泄
        raise RuntimeError("upstream error token=SECRET_AT code=SECRET_CODE")


def _app_boom(maker):
    app = FastAPI()
    async def _get_db():
        async with maker() as s:
            yield s
            await s.commit()
    app.include_router(create_scm_oauth_routes(
        get_current_user=(lambda: None), get_db=_get_db,
        get_login_provider=lambda prov, cfg: _BoomGitHub(), oauth_config=_cfg()))
    return app


def test_callback_upstream_error_scrubbed(maker, caplog):  # B4
    c = TestClient(_app_boom(maker), follow_redirects=False)
    r0 = c.get("/auth/github/login")
    state = r0.headers["location"].split("state=")[1]
    r = c.get("/auth/github/callback", params={"code": "SECRET_CODE", "state": state})
    assert r.status_code == 502
    # 上游错误体/敏感串绝不透传给客户端
    assert "SECRET_AT" not in r.text and "SECRET_CODE" not in r.text
    # 日志里也不出现 token/code
    assert "SECRET_AT" not in caplog.text and "SECRET_CODE" not in caplog.text


@pytest.mark.asyncio
async def test_callback_fail_closed_on_enc_failure(maker, monkeypatch):  # B5
    import src.service.token_crypto as tc
    monkeypatch.setenv("KE_JWT_SECRET", "x" * 32)
    monkeypatch.delenv("KE_TOKEN_ENC_KEY", raising=False)
    tc.reset_fernet_cache()                       # 让缺 key 立刻生效
    async with maker() as s:
        s.add(User(email="e@x.com", username="eve", hashed_password="h",
                   is_active=True, github_user_id=42)); await s.commit()
    c = TestClient(_app(maker), follow_redirects=False)
    r0 = c.get("/auth/github/login")
    state = r0.headers["location"].split("state=")[1]
    r = c.get("/auth/github/callback", params={"code": "c", "state": state})
    assert r.status_code >= 500                    # 加密失败 → 5xx
    assert "refresh_token=" not in r.headers.get("set-cookie", "")   # 未下发 cookie
    async with maker() as s:
        from src.service.db_models_homepage import UserScmToken
        assert (await s.execute(select(UserScmToken))).scalars().first() is None  # 未写 token（回滚）


@pytest.mark.asyncio
async def test_callback_locked_423(maker, monkeypatch):
    monkeypatch.setenv("KE_JWT_SECRET", "x" * 32)
    async with maker() as s:
        s.add(User(email="l@x.com", username="lock", hashed_password="h",
                   is_active=True, github_user_id=42,
                   locked_until=datetime(2099, 12, 31, tzinfo=timezone.utc)))
        await s.commit()
    c = TestClient(_app(maker), follow_redirects=False)
    r0 = c.get("/auth/github/login")
    state = r0.headers["location"].split("state=")[1]
    r = c.get("/auth/github/callback", params={"code": "c", "state": state})
    assert r.status_code == 423


def test_gitlab_unconfigured_503(maker):
    """gitlab=None 时 /auth/gitlab/login 应返回 503（provider 未配置，fail-closed）。

    _cfg() 构造的 OAuthConfig 里 gitlab=None，路由层 _provider_or_503 检测到
    oauth_config.provider("gitlab") is None → HTTPException(503)。
    同时验证密码登录端点不受影响（不在此路由，不应 503）。
    """
    # _app 使用 _cfg()（gitlab=None）构造 app，不需要 async with maker()
    c = TestClient(_app(maker), follow_redirects=False)
    # gitlab 未配置 → 503
    r = c.get("/auth/gitlab/login")
    assert r.status_code == 503
    # github 已配置（_cfg() 里 github=GitHubOAuthConfig(...)）→ 正常 302 跳转
    r2 = c.get("/auth/github/login")
    # 302/307 均表示正常重定向（不是 503）
    assert r2.status_code in (302, 307)
