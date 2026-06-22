# tests/test_auth/test_scm_oauth_link.py
"""OAuth link 分支 + link-scm start + scm-links GET/DELETE 的测试。设计 §4.4。"""
import pytest, pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select
from src.service.db import Base
from src.service.auth_models import User
from src.service.db_models_homepage import UserScmToken
from src.service.scm.config import OAuthConfig, GitHubOAuthConfig
from src.service.scm.base import ScmIdentity
from src.service.scm_oauth_router import create_scm_oauth_routes


@pytest_asyncio.fixture
async def maker():
    # 每个测试独立内存 DB，避免状态污染
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


class _FakeGitHub:
    """伪造的 GitHub OAuth Provider，控制返回 uid/login，无需真实网络。"""
    def __init__(self, uid="42", login="octocat"):
        self._uid = uid; self._login = login

    def build_authorize_url(self, *, client_id, redirect_uri, state):
        # 返回带 state 的假 URL，方便测试提取 state 值
        return f"https://gh/auth?state={state}"

    async def exchange_code(self, *, client_id, client_secret, code, redirect_uri):
        # 模拟 GitHub 换 code 返回 access_token
        return {"access_token": "AT", "scope": "read:user"}

    async def get_login_identity(self, token):
        # 返回 ScmIdentity，scm_user_id 始终是字符串（router 内部负责归一类型）
        return ScmIdentity(provider="github", scm_user_id=self._uid, login=self._login)


def _cfg():
    """最小化 OAuth 配置：只启用 GitHub，redirect_base 为 https://ke。"""
    return OAuthConfig(redirect_base="https://ke", github=GitHubOAuthConfig("cid", "sec"), gitlab=None)


def _app(maker, current_user, fake=None):
    """构造带 SCM OAuth 路由的 FastAPI 测试应用。

    Args:
        maker:        async_sessionmaker，提供 DB 会话
        current_user: 注入的当前登录用户对象（模拟已登录态）
        fake:         可选的假 provider；默认用 _FakeGitHub()
    """
    app = FastAPI()
    # get_db 依赖：每次请求生成一个新 session 并在结束时 commit
    async def _get_db():
        async with maker() as s:
            yield s; await s.commit()
    # get_current_user 返回 lambda 本身（Depends 会调用它）
    app.include_router(create_scm_oauth_routes(
        get_current_user=lambda: current_user, get_db=_get_db,
        get_login_provider=lambda prov, cfg: fake or _FakeGitHub(), oauth_config=_cfg()))
    return app


@pytest.mark.asyncio
async def test_link_flow_writes_identity(maker):
    """link 完整流程：start→callback，User.github_user_id 和 UserScmToken 都写入。"""
    # 先插入用户，拿到自增 id
    async with maker() as s:
        u = User(email="a@x.com", username="alice", hashed_password="h", is_active=True)
        s.add(u); await s.commit(); uid = u.id
    # 用轻量 type() 构造符合 get_current_user 接口的用户对象
    user = type("U", (), {"id": uid, "username": "alice", "is_admin": False})()
    c = TestClient(_app(maker, user), follow_redirects=False)
    # Step 1：POST /account/link-scm/github/start → 返回 authorize_url
    r0 = c.post("/account/link-scm/github/start")
    assert r0.status_code == 200 and "authorize_url" in r0.json()
    # 从 authorize_url 提取 state（假 provider 把 state 拼在 query 里）
    state = r0.json()["authorize_url"].split("state=")[1]
    # Step 2：GET /auth/github/callback → link 分支写身份 + token，302 跳转
    r = c.get("/auth/github/callback", params={"code": "c", "state": state})
    assert r.status_code in (200, 302)
    # 验证数据库：github_user_id 写成整型 42，UserScmToken 有 scm_login
    async with maker() as s:
        row = (await s.execute(select(User).where(User.id == uid))).scalar_one()
        assert row.github_user_id == 42
        assert (await s.execute(select(UserScmToken).where(UserScmToken.user_id == uid))).scalar_one().scm_login == "octocat"


@pytest.mark.asyncio
async def test_link_conflict_other_user_409(maker):
    """他人已绑同一 GitHub uid → 409（不能抢占）。"""
    async with maker() as s:
        # other 已经绑了 github_user_id=42
        other = User(email="o@x.com", username="other", hashed_password="h", github_user_id=42)
        me = User(email="a@x.com", username="alice", hashed_password="h")
        s.add_all([other, me]); await s.commit(); uid = me.id
    user = type("U", (), {"id": uid, "username": "alice", "is_admin": False})()
    c = TestClient(_app(maker, user), follow_redirects=False)
    # _FakeGitHub 默认返回 uid="42"，与 other 的 github_user_id 冲突
    state = c.post("/account/link-scm/github/start").json()["authorize_url"].split("state=")[1]
    r = c.get("/auth/github/callback", params={"code": "c", "state": state})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_link_self_different_account_409(maker):
    """自己已绑不同的 GitHub uid → 409（防止静默替换身份）。"""
    async with maker() as s:
        # me 已绑 github_user_id=99，但 fake provider 返回 uid=42
        me = User(email="a@x.com", username="alice", hashed_password="h", github_user_id=99)
        s.add(me); await s.commit(); uid = me.id
    user = type("U", (), {"id": uid, "username": "alice", "is_admin": False})()
    # 传入 fake=_FakeGitHub(uid="42")，与 DB 里的 99 不同
    c = TestClient(_app(maker, user, fake=_FakeGitHub(uid="42")), follow_redirects=False)
    state = c.post("/account/link-scm/github/start").json()["authorize_url"].split("state=")[1]
    r = c.get("/auth/github/callback", params={"code": "c", "state": state})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_scm_links_get_and_delete(maker):
    """GET /account/scm-links 返回已关联列表；DELETE 幂等 204；DB 行清除。"""
    async with maker() as s:
        u = User(email="a@x.com", username="alice", hashed_password="h", github_user_id=42)
        s.add(u); await s.commit(); uid = u.id
        from datetime import datetime
        # 预置一条 UserScmToken 行，linked_at 填固定值
        s.add(UserScmToken(id="t1", user_id=uid, provider="github", access_token="enc",
                           scm_login="octocat", linked_at=datetime(2026, 6, 21))); await s.commit()
    user = type("U", (), {"id": uid, "username": "alice", "is_admin": False})()
    c = TestClient(_app(maker, user), follow_redirects=False)
    # GET：应返回 github 关联信息
    links = c.get("/account/scm-links").json()["links"]
    assert any(l["provider"] == "github" and l["scm_login"] == "octocat" for l in links)
    # DELETE：返回 204，DB 行清除
    assert c.delete("/account/scm-links/github").status_code == 204
    async with maker() as s:
        assert (await s.execute(select(User).where(User.id == uid))).scalar_one().github_user_id is None
        assert (await s.execute(select(UserScmToken).where(UserScmToken.user_id == uid))).scalar_one_or_none() is None
    # 幂等：再删一次仍 204（不报错）
    assert c.delete("/account/scm-links/github").status_code == 204
