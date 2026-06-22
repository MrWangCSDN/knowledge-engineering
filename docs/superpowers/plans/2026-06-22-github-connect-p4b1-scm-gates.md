# GitHub 连接 P4b-1：SCM 门接线（bind + QA can_query + authorize_scm + kill-switch）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL：superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans，逐任务执行。Steps 用 `- [ ]`。

**Goal:** 在 P4b-0 provider 基座上接上 SCM 门——bind 校验 can_bind（实时不缓存）、QA `/explain` 校验 can_query（缓存）、共享 `authorize_scm` helper、双默认关 kill-switch、连接删除清缓存。

**Architecture:** 新 `scm/scm_authz.py` 装配 provider→token→resolve 链并把 infra 错抛成 HTTPException（403/502/503）、返回 `ScmRole`；bind 门走工厂注入参（`scm_binding_router`）、QA 门走 `app.state.authorize_scm` 接缝（`qa_router` 是单例）。两门各自 kill-switch（`KE_SCM_BIND_AUTHZ`/`KE_SCM_QA_AUTHZ`，**默认关→合并零行为变化**）。PAT 连接(`auth_type=="pat"`)与 unbound 工程→纯 KE-RBAC。

**Tech Stack:** FastAPI / httpx / pytest + pytest-httpx / asyncio.wait_for。复用 P4b-0 `resolve_repo_role`(裸/`resolve_repo_role_cached`)、`build_refresh_fn`、`scm_perm_cache.cache_invalidate`；P4a `get_valid_scm_token`/`ScmTokenInvalid`/`UserScmToken`/`get_login_provider`/`OAuthConfig`。

**设计依据:** [[GitHub仓库连接-P4b-1-SCM门接线-设计]]（spec，过对抗评审）。

**前置:** P1–P4a + P4b-0 已在本分支。worktree `/Users/java/ke-github-connect`，分支 `feat/github-repo-connect`。测试统一前缀：
```
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest <path> -v
```

**关键既有 API/锚点（勿臆造）:**
- `ScmRole`（`scm/base.py:11-15`）：`CAN_BIND`/`CAN_QUERY`/`NOT_VISIBLE`。
- `get_login_provider(provider, oauth_cfg)`（`scm/oauth_factory.py`）→ provider 实例 / 抛 `OAuthProviderUnavailable`。
- `build_refresh_fn(provider, *, gitlab_provider=None, oauth_cfg=None)`（`scm/scm_refresh.py`）。
- `get_valid_scm_token(db, *, user_id, provider, refresh_fn) -> str` / `ScmTokenInvalid`（`scm/scm_token_store.py`）。
- `resolve_repo_role_cached(provider_obj, *, user_id, connection_id, repo_external_id, token, repo, principal)`（`scm/scm_perm_cache.py`）；`cache_invalidate(connection_id)` 同模块。
- provider `resolve_repo_role(*, token, repo, principal)`（github repo=full_name / gitlab repo=int project_id, principal=gitlab_sub）。
- `UserScmToken`（`db_models_homepage.py`）字段 `user_id/provider/scm_login`；`ScmConnection.provider`/`.auth_type`（`:190`，值 `github_app`/`pat`）/`.github_installation_id`。
- `OAuthConfig`/`GitHubOAuthConfig`/`GitLabOidcConfig` + `load_oauth_config()`（`scm/config.py`）。
- `qa_router.explain`（`qa_router.py:263`）：有 `request: Request`(266)、`user`(268,`user.id`)、`db`(267)、`p=await db.get(ProjectModel, project_id)`(278)、404(279-280)、409 状态门(281-285)、QASession 创建+commit(300-310)、稍后 `StreamingResponse`。**门插入点 = 第 286 行（409 之后、会话块之前）**。`request.app.state` 已被 explain 用于取 synthesizer/retriever。
- `scm_binding_router.py`：bind 路由 TODO(P4) 行；BindRequest 字段 `connection_id/repo_full_name/repo_external_id`；工厂 `create_scm_binding_routes(*, get_current_user, get_db, require_role=require_project_role)`；`api.py` 挂载不传额外 kwargs（新参须默认值）。
- `scm_router.py delete_connection`：`await db.delete(conn)` → `await db.commit()` → `return Response(status_code=204)`。
- kill-switch 范式：`os.environ.get("KE_QA_USE_REACT","").strip()...`（per-request）。

**范围边界:** 不做 P4b-2 callback 归属、P4c visible-repos、unlink/re-link 缓存失效、session 读/导出门。**flag 默认关**。

---

## 文件结构
| 文件 | 责任 |
|---|---|
| `src/service/scm/scm_authz.py`（新） | `flag_on` + `create_authorize_scm`（provider→token→resolve，抛 HTTPException/返 ScmRole） |
| `src/service/scm_binding_router.py`（改） | bind 门（工厂参 `authorize_scm=None` + flag + PAT 判 auth_type） |
| `src/service/qa_router.py`（改） | `/explain` can_query 门（`app.state.authorize_scm` + flag，插在会话创建前） |
| `src/service/scm_router.py`（改） | `delete_connection` commit 后 `cache_invalidate` |
| `src/service/api.py`（改） | 造 `authorize_scm` → `app.state.authorize_scm` + 注入 bind 工厂 |
| `tests/test_auth/test_scm_authz.py` / `test_scm_binding_gate.py` / `test_qa_scm_gate.py` / `test_conn_delete_invalidate.py` | 测试 |

---

## Task 1：`scm_authz.py`（authorize_scm + flag_on）

**Files:** Create `src/service/scm/scm_authz.py`；Test `tests/test_auth/test_scm_authz.py`

- [ ] **Step 1: 失败测试**
```python
# tests/test_auth/test_scm_authz.py
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
```

- [ ] **Step 2: 跑测试确认失败**（ImportError `scm_authz`）。

- [ ] **Step 3: 实现**（spec §4.1 逐字）
```python
# src/service/scm/scm_authz.py
"""SCM 门核心：装配 provider→token→resolve，infra 错抛 HTTPException、返回 ScmRole。设计 §4.1。"""
from __future__ import annotations

import asyncio
import os
import httpx
from typing import Callable, Optional

from fastapi import HTTPException
from sqlalchemy import select

from src.service.scm.base import ScmRole
from src.service.scm.oauth_factory import OAuthProviderUnavailable
from src.service.scm.scm_refresh import build_refresh_fn
from src.service.scm.scm_token_store import get_valid_scm_token, ScmTokenInvalid
from src.service.db_models_homepage import UserScmToken
from src.service.scm.scm_perm_cache import resolve_repo_role_cached


def flag_on(env_name: str) -> bool:
    """per-request 读 kill-switch（沿用 KE_QA_USE_REACT 范式，额外 case-fold）。两路由共用。"""
    return os.environ.get(env_name, "").strip().lower() in {"1", "true", "yes"}


def create_authorize_scm(*, oauth_cfg, get_login_provider: Callable, scm_timeout: float = 8.0):
    async def authorize_scm(db, *, user, conn, repo_full_name, repo_external_id,
                            need_bind: bool) -> ScmRole:
        """解析 user 在 conn 所指仓的 ScmRole；infra 错抛 HTTPException，成功返 ScmRole（含 NOT_VISIBLE，路由判档）。
        前提：调用方已判 kill-switch on、conn.auth_type != 'pat'、工程已绑定。"""
        provider_name = conn.provider
        try:
            provider_obj = get_login_provider(provider_name, oauth_cfg)
        except OAuthProviderUnavailable:
            raise HTTPException(status_code=503, detail=f"{provider_name} 未配置")
        if provider_name == "github":
            row = (await db.execute(select(UserScmToken).where(
                UserScmToken.user_id == user.id, UserScmToken.provider == "github"))).scalar_one_or_none()
            principal = row.scm_login if row else None
            repo_arg = repo_full_name
        else:
            principal = str(user.gitlab_sub) if getattr(user, "gitlab_sub", None) is not None else None
            repo_arg = repo_external_id
        if not principal:
            raise HTTPException(status_code=403, detail="请先关联对应 SCM 账号")
        refresh_fn = build_refresh_fn(
            provider_name,
            gitlab_provider=provider_obj if provider_name == "gitlab" else None,
            oauth_cfg=oauth_cfg)
        try:
            token = await get_valid_scm_token(db, user_id=user.id, provider=provider_name, refresh_fn=refresh_fn)
        except ScmTokenInvalid:
            raise HTTPException(status_code=403, detail="SCM 授权已失效，请重新关联")
        except (httpx.HTTPError, RuntimeError):
            raise HTTPException(status_code=502, detail="SCM 授权刷新失败，请重试")
        try:
            if need_bind:
                role = await asyncio.wait_for(
                    provider_obj.resolve_repo_role(token=token, repo=repo_arg, principal=principal), scm_timeout)
            else:
                role = await asyncio.wait_for(
                    resolve_repo_role_cached(provider_obj, user_id=user.id, connection_id=conn.id,
                        repo_external_id=repo_external_id, token=token, repo=repo_arg, principal=principal),
                    scm_timeout)
        except (asyncio.TimeoutError, httpx.HTTPError):
            raise HTTPException(status_code=502, detail="SCM 权限校验失败，请重试")
        return role
    return authorize_scm
```

- [ ] **Step 4: 跑测试确认通过**（8 passed）。

- [ ] **Step 5: 提交**
```bash
git add src/service/scm/scm_authz.py tests/test_auth/test_scm_authz.py && git commit -m "feat(scm): authorize_scm 门核心（provider→token→resolve，infra→HTTPException）+ flag_on"
```

---

## Task 2：bind 门（scm_binding_router）

**Files:** Modify `src/service/scm_binding_router.py`；Test `tests/test_auth/test_scm_binding_gate.py`

> 工厂加默认参 `authorize_scm: Optional[Callable] = None`（不破 api.py）。门在 `# TODO(P4)` 行（mutate 之前、读 `body.*`）。flag off / authorize_scm None → 不触发（**现有 test_scm_binding/rbac 零回归**）。
> **I4 刻意不对称（勿"统一"）**：bind 对缺失连接返 **404**（`body.connection_id` 是调用者可塞的，必须存在）；QA 门读的是已校验的 `p.scm_connection_id`，缺/PAT 走 KE-RBAC 回退、**永不 404**。两者语义不同，别归一。

- [ ] **Step 1: 失败测试**
```python
# tests/test_auth/test_scm_binding_gate.py
import pytest, pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from src.service.db import Base
from src.service.db_models_homepage import Project, ScmConnection
from src.service.scm.base import ScmRole
from src.service.scm_binding_router import create_scm_binding_routes


class _User:
    id = 1; username = "alice"; is_admin = True


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


def _app(maker, *, authorize_scm=None):
    app = FastAPI()
    async def _get_db():
        async with maker() as s:
            yield s
    app.include_router(create_scm_binding_routes(
        get_current_user=lambda: _User(), get_db=_get_db,
        require_role=lambda role: (lambda: None), authorize_scm=authorize_scm))
    return app


_BODY = {"connection_id": "c1", "repo_external_id": 42, "repo_full_name": "o/r",
         "ref": "master", "ref_type": "branch", "subpath": None}


async def _seed(maker, *, auth_type="github_app"):
    async with maker() as s:
        s.add(Project(id="p1", name="P1"))
        s.add(ScmConnection(id="c1", provider="github", auth_type=auth_type,
                            github_installation_id=(None if auth_type == "pat" else 7),
                            account_login="o", status="active", created_by="alice"))
        await s.commit()


@pytest.mark.asyncio
async def test_flag_off_no_gate(maker, monkeypatch):
    monkeypatch.delenv("KE_SCM_BIND_AUTHZ", raising=False)
    async with maker() as s:
        s.add(Project(id="p1", name="P1")); await s.commit()   # 无连接也 200（同今天）
    async def _boom(*a, **k): raise AssertionError("gate should not run")
    c = TestClient(_app(maker, authorize_scm=_boom))
    assert c.post("/projects/p1/bind", json=_BODY).status_code == 200


@pytest.mark.asyncio
async def test_flag_on_can_bind_200(maker, monkeypatch):
    monkeypatch.setenv("KE_SCM_BIND_AUTHZ", "1")
    await _seed(maker)
    async def _authz(db, **k): return ScmRole.CAN_BIND
    c = TestClient(_app(maker, authorize_scm=_authz))
    assert c.post("/projects/p1/bind", json=_BODY).status_code == 200


@pytest.mark.asyncio
async def test_flag_on_can_query_403(maker, monkeypatch):
    monkeypatch.setenv("KE_SCM_BIND_AUTHZ", "1")
    await _seed(maker)
    async def _authz(db, **k): return ScmRole.CAN_QUERY
    c = TestClient(_app(maker, authorize_scm=_authz))
    assert c.post("/projects/p1/bind", json=_BODY).status_code == 403


@pytest.mark.asyncio
async def test_flag_on_missing_conn_404(maker, monkeypatch):
    monkeypatch.setenv("KE_SCM_BIND_AUTHZ", "1")
    async with maker() as s:
        s.add(Project(id="p1", name="P1")); await s.commit()   # 无连接行
    async def _authz(db, **k): return ScmRole.CAN_BIND
    c = TestClient(_app(maker, authorize_scm=_authz))
    assert c.post("/projects/p1/bind", json=_BODY).status_code == 404


@pytest.mark.asyncio
async def test_flag_on_pat_skips_gate(maker, monkeypatch):
    monkeypatch.setenv("KE_SCM_BIND_AUTHZ", "1")
    await _seed(maker, auth_type="pat")
    async def _boom(*a, **k): raise AssertionError("PAT should skip SCM gate")
    c = TestClient(_app(maker, authorize_scm=_boom))
    assert c.post("/projects/p1/bind", json=_BODY).status_code == 200
```

- [ ] **Step 2: 跑测试确认失败**（authorize_scm 不是工厂参 → TypeError）。

- [ ] **Step 3: 实现**
工厂签名（`scm_binding_router.py`）改为加默认参：
```python
def create_scm_binding_routes(*, get_current_user: Callable, get_db: Callable,
                              require_role: Callable = require_project_role,
                              authorize_scm: Optional[Callable] = None) -> APIRouter:
```
顶部 import 补：`from src.service.scm.scm_authz import flag_on`、`from src.service.scm.base import ScmRole`、`from src.service.db_models_homepage import ScmConnection`（若未 import）。在 bind 的 `# TODO(P4)：SCM-role 门禁...` 处替换为：
```python
        if flag_on("KE_SCM_BIND_AUTHZ") and authorize_scm is not None:
            conn = await db.get(ScmConnection, body.connection_id)
            if conn is None:
                raise HTTPException(status_code=404, detail="连接不存在")
            if conn.auth_type != "pat":          # PAT → 跳过 SCM 门（纯 KE-RBAC）
                role = await authorize_scm(db, user=user, conn=conn,
                    repo_full_name=body.repo_full_name, repo_external_id=body.repo_external_id, need_bind=True)
                if role != ScmRole.CAN_BIND:
                    raise HTTPException(status_code=403, detail="无该仓 maintainer/admin 权限，不能绑定")
```
（`Optional`/`Callable` 已在 typing import；`HTTPException` 已 import。）

- [ ] **Step 4: 跑测试确认通过**（5 passed）+ **现有 bind 回归**：
```
cd /Users/java/ke-github-connect && KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth/test_scm_binding.py tests/test_auth/test_scm_binding_rbac.py tests/test_auth/test_scm_binding_gate.py -v
```
全过（旧测试 flag 默认关 → 零回归）。

- [ ] **Step 5: 提交**
```bash
git add src/service/scm_binding_router.py tests/test_auth/test_scm_binding_gate.py && git commit -m "feat(scm): bind 门（KE_SCM_BIND_AUTHZ + authorize_scm，PAT 判 auth_type，默认关零回归）"
```

---

## Task 3：QA `/explain` can_query 门（qa_router）

**Files:** Modify `src/service/qa_router.py`；Test `tests/test_auth/test_qa_scm_gate.py`

> 门走 `app.state.authorize_scm`（qa_router 是单例）。插入点 = `qa_router.py:286`（409 状态门之后、第 287 行会话块/`QASession` 创建之前 → 403 不留孤儿会话）。

- [ ] **Step 1: 失败测试**
```python
# tests/test_auth/test_qa_scm_gate.py
import pytest, pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select
from src.service import auth_security as sec
from src.service.auth_models import User
from src.service.auth_router import router as auth_router
from src.service.db import Base, get_db
from src.service.deps_infra import require_infra_healthy
from src.service.db_models_homepage import Project, ScmConnection, QASession
from src.service.qa_router import router as qa_router
from src.service.scm.base import ScmRole


@pytest_asyncio.fixture
async def session_maker(monkeypatch):
    monkeypatch.setenv("KE_JWT_SECRET", "x" * 32)
    monkeypatch.setenv("KE_COOKIE_SECURE", "false")
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    SM = async_sessionmaker(eng, expire_on_commit=False)
    async with SM() as s:
        s.add(User(email="a@x.com", username="alice", hashed_password=sec.hash_password("12345678"),
                   is_active=True, is_admin=True))
        # bound 工程 p1 + 连接 c1（github_app）
        s.add(Project(id="p1", name="P1", status="ready", scm_connection_id="c1",
                      repo_external_id=42, repo_full_name="o/r"))
        s.add(ScmConnection(id="c1", provider="github", auth_type="github_app",
                            github_installation_id=7, account_login="o", status="active", created_by="alice"))
        # unbound 工程 p2
        s.add(Project(id="p2", name="P2", status="ready"))
        # GitLab 绑定工程 pg + 连接 cg（非 PAT；I3/B1 回归用）
        s.add(Project(id="pg", name="PG", status="ready", scm_connection_id="cg",
                      repo_external_id=99, repo_full_name="g/r"))
        s.add(ScmConnection(id="cg", provider="gitlab", auth_type="github_app",
                            github_installation_id=None, account_login="g", status="active", created_by="alice"))
        await s.commit()
    return SM


def _client(session_maker, *, authorize_scm=None):
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(qa_router)
    async def override_db():
        async with session_maker() as s:
            yield s
            await s.commit()
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[require_infra_healthy] = lambda: None   # B2：否则 router 级 503 在门之前拦住
    if authorize_scm is not None:
        app.state.authorize_scm = authorize_scm
    return TestClient(app)


def _login(c):
    r = c.post("/auth/login", json={"username": "alice", "password": "12345678", "remember_me": False})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _ask(c, pid, hdr):
    # B1：真实路由前缀是 /projects/{id}/qa（qa_router prefix）+ /explain
    return c.post(f"/projects/{pid}/qa/explain", json={"question": "什么是订单超时"}, headers=hdr)


def test_flag_off_no_gate(session_maker, monkeypatch):
    monkeypatch.delenv("KE_SCM_QA_AUTHZ", raising=False)
    async def _boom(*a, **k): raise AssertionError("gate should not run")
    c = _client(session_maker, authorize_scm=_boom)
    hdr = _login(c)
    # flag off → 不进门。过门后引擎未就绪 → 503（fixture 不挂引擎）。in (200,503) 同时排除 403(误触门) 与 500(_boom 误调)
    assert _ask(c, "p1", hdr).status_code in (200, 503)


def test_flag_on_not_visible_403_no_session(session_maker, monkeypatch):
    monkeypatch.setenv("KE_SCM_QA_AUTHZ", "1")
    async def _authz(db, **k): return ScmRole.NOT_VISIBLE
    c = _client(session_maker, authorize_scm=_authz)
    hdr = _login(c)
    r = _ask(c, "p1", hdr)
    assert r.status_code == 403
    assert r.headers["content-type"].startswith("application/json")   # JSON 非流


@pytest.mark.asyncio
async def test_flag_on_not_visible_leaves_no_qasession(session_maker, monkeypatch):
    monkeypatch.setenv("KE_SCM_QA_AUTHZ", "1")
    async def _authz(db, **k): return ScmRole.NOT_VISIBLE
    c = _client(session_maker, authorize_scm=_authz)
    hdr = _login(c)
    _ask(c, "p1", hdr)
    async with session_maker() as s:
        rows = (await s.execute(select(QASession).where(QASession.project_id == "p1"))).scalars().all()
    assert rows == []   # 403 在会话创建前 → 无孤儿


def test_flag_on_unbound_skips(session_maker, monkeypatch):
    monkeypatch.setenv("KE_SCM_QA_AUTHZ", "1")
    async def _boom(*a, **k): raise AssertionError("unbound should skip gate")
    c = _client(session_maker, authorize_scm=_boom)
    hdr = _login(c)
    assert _ask(c, "p2", hdr).status_code in (200, 503)   # unbound → 跳过 SCM 门（_boom 未被调）


def test_flag_on_can_query_passes_gate(session_maker, monkeypatch):
    monkeypatch.setenv("KE_SCM_QA_AUTHZ", "1")
    async def _authz(db, **k): return ScmRole.CAN_QUERY
    c = _client(session_maker, authorize_scm=_authz)
    hdr = _login(c)
    # 过门 → 引擎未就绪 503（注：CAN_QUERY 过门后会先建 QASession 再 503，会留一条会话，符合预期，不断言无孤儿）
    assert _ask(c, "p1", hdr).status_code in (200, 503)


def test_flag_on_gitlab_connection_gate_fires(session_maker, monkeypatch):
    # I3 / B1 回归：非 PAT 的 GitLab 连接（fixture 已 seed pg/cg）**必须**进门、不被当 PAT 跳过
    monkeypatch.setenv("KE_SCM_QA_AUTHZ", "1")
    async def _authz(db, **k): return ScmRole.NOT_VISIBLE
    c = _client(session_maker, authorize_scm=_authz)
    hdr = _login(c)
    assert _ask(c, "pg", hdr).status_code == 403   # GitLab 连接进门、NOT_VISIBLE → 403（证明未被 PAT 跳过）
```

- [ ] **Step 2: 跑测试确认失败**（门未加 → `test_flag_on_not_visible_403` 不得 403）。

- [ ] **Step 3: 实现**（`qa_router.py`）。**插入点（结构锚，勿用裸行号）**：紧跟 **indexing-409** 的 `raise`（qa_router.py:285，`if p.status == "indexing"` 那个）之后、`# 2. 复用会话或新建` 注释（:287）之前——即在 **archived-session 409 检查（:296）与 `if is_new_session:` 的 QASession 创建（:300-310）之前**。这保证 NOT_VISIBLE→403 时不会先建会话（无孤儿）。注意 handler 有**两个** 409（indexing / archived-session），别落到第二个里。
```python
    # P4b-1：SCM can_query 门（仅 /explain；unbound/PAT → 纯 KE-RBAC；门在会话创建前→403 不留孤儿）
    authorize_scm = getattr(request.app.state, "authorize_scm", None)
    if flag_on("KE_SCM_QA_AUTHZ") and p.scm_connection_id and authorize_scm is not None:
        conn = await db.get(ScmConnection, p.scm_connection_id)
        if conn is not None and conn.auth_type != "pat":
            role = await authorize_scm(db, user=user, conn=conn,
                repo_full_name=p.repo_full_name, repo_external_id=p.repo_external_id, need_bind=False)
            if role not in (ScmRole.CAN_QUERY, ScmRole.CAN_BIND):
                raise HTTPException(status_code=403, detail="无该仓读取权限")
```
顶部 import 补：`from src.service.scm.scm_authz import flag_on`、`from src.service.scm.base import ScmRole`、`from src.service.db_models_homepage import ScmConnection`（若未 import；`QASession`/`ProjectModel` 已 import）。

- [ ] **Step 4: 跑测试确认通过**（6 passed：含 GitLab 连接进门回归）+ **现有 QA 回归**：
```
cd /Users/java/ke-github-connect && KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth/test_qa_scm_gate.py tests/test_auth/test_qa_session_router.py -v
```
（QA flag 默认关 → 现有 qa 测试零回归。）

- [ ] **Step 5: 提交**
```bash
git add src/service/qa_router.py tests/test_auth/test_qa_scm_gate.py && git commit -m "feat(scm): QA /explain can_query 门（app.state.authorize_scm + KE_SCM_QA_AUTHZ，门在会话创建前）"
```

---

## Task 4：api.py 装配 + 连接删除清缓存 + 全回归

**Files:** Modify `src/service/api.py`、`src/service/scm_router.py`；Test `tests/test_auth/test_conn_delete_invalidate.py`

- [ ] **Step 1: 失败测试（连接删除清缓存，效果断言）**
```python
# tests/test_auth/test_conn_delete_invalidate.py
import pytest, pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from src.service.db import Base
from src.service.db_models_homepage import ScmConnection
from src.service.scm_router import create_scm_routes
from src.service.scm import scm_perm_cache


class _User:
    username = "alice"; is_admin = True


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_delete_connection_invalidates_cache(maker):
    scm_perm_cache.cache_clear()
    scm_perm_cache._CACHE[(1, "c1", 42)] = ("can_query", 9e18)   # 占位正向缓存项
    async with maker() as s:
        s.add(ScmConnection(id="c1", provider="github", auth_type="github_app",
                            github_installation_id=1, account_login="o", status="active", created_by="alice"))
        await s.commit()
    app = FastAPI()
    async def _get_db():
        async with maker() as s:
            yield s
    app.include_router(create_scm_routes(get_current_user=lambda: _User(), get_db=_get_db,
                                         get_provider=lambda: None, app_slug="x"))
    c = TestClient(app)
    assert c.delete("/scm/connections/c1").status_code == 204
    assert not any(k[1] == "c1" for k in scm_perm_cache._CACHE)   # 该连接缓存已清
    scm_perm_cache.cache_clear()
```

- [ ] **Step 2: 跑测试确认失败**（删除后缓存仍在）。

- [ ] **Step 3a: scm_router delete_connection 加 cache_invalidate**
`src/service/scm_router.py` 顶部 import 补 `from src.service.scm.scm_perm_cache import cache_invalidate`。在 `delete_connection` 的 `await db.commit()` 之后、`return Response(status_code=204)` 之前：
```python
        try:
            cache_invalidate(connection_id)      # best-effort：清该连接 can_query 缓存
        except Exception:                        # noqa: BLE001 — 不让缓存清理 500 掉删除
            pass
```

- [ ] **Step 3b: api.py 装配 authorize_scm**
在 `api.py`（scm 路由装配区，`load_oauth_config`/`get_login_provider` 已 import；若 `create_authorize_scm` 未 import 则补）：
```python
from src.service.scm.scm_authz import create_authorize_scm
_authorize_scm = create_authorize_scm(oauth_cfg=load_oauth_config(), get_login_provider=get_login_provider)
app.state.authorize_scm = _authorize_scm          # QA 门经 app.state 取
```
并把现有 `app.include_router(create_scm_binding_routes(get_current_user=get_current_user, get_db=get_db))` 改为传入 `authorize_scm=_authorize_scm`：
```python
app.include_router(create_scm_binding_routes(
    get_current_user=get_current_user, get_db=get_db, authorize_scm=_authorize_scm))
```
（`load_oauth_config()` import 期执行、未配 provider 返 None 不抛，故 import 冒烟不需 OAuth env。）

- [ ] **Step 4: 跑测试 + import 冒烟**
```
cd /Users/java/ke-github-connect && KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth/test_conn_delete_invalidate.py -v
cd /Users/java/ke-github-connect && KE_JWT_SECRET=test KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -c "import src.service.api; print('api OK')"
```

- [ ] **Step 5: P4b-1 全回归 + 全量 test_auth**
```
cd /Users/java/ke-github-connect && KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest \
  tests/test_auth/test_scm_authz.py tests/test_auth/test_scm_binding_gate.py \
  tests/test_auth/test_qa_scm_gate.py tests/test_auth/test_conn_delete_invalidate.py -v
cd /Users/java/ke-github-connect && KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth/ -q
```
Expected：P4b-1 全过；全量 test_auth 绿（**flag 默认关→零回归**，仅既有 sqlite teardown 噪声）。报告确切 pass/fail 数。

- [ ] **Step 6: 提交**
```bash
git add src/service/api.py src/service/scm_router.py tests/test_auth/test_conn_delete_invalidate.py && git commit -m "feat(scm): 装配 authorize_scm(app.state+bind 工厂) + 连接删除清权限缓存（P4b-1 收尾）"
```

---

## 完成标准（P4b-1 Done，对齐 spec §8）
1. `authorize_scm`：principal 分 provider（github=scm_login/gitlab=sub）；缺 token/失效→403、未配→503、5xx/ConnectError/超时→502；need_bind 裸 vs cached。
2. bind 门：flag off 零变化；on 时 CAN_BIND→200 / 非→403 / 连接不存在→404 / PAT→跳过。
3. QA 门：flag off 零变化；on 时 unbound 跳过 / NOT_VISIBLE→403（JSON、无孤儿 QASession）/ CAN_QUERY 过门。
4. 连接删除→`cache_invalidate`（效果断言：该 connection_id 缓存项消失）。
5. 全量 test_auth 绿（**默认关零回归**）+ import api 冒烟过。

## 待后续
- **P4b-2**：App-install callback 严格归属核验（install-先于-link 矛盾）。
- 后续策略片：session 读/导出门；unlink/re-link 缓存失效（建 user_id→connection_id 桥）；跨进程缓存失效。
- **上线门**：核实 GitLab sub=numeric user id、GitHub 只读 403 语义；翻 flag 前对 GitHub+GitLab 各验一个无权用户确 403；先 `KE_SCM_BIND_AUTHZ` 再 `KE_SCM_QA_AUTHZ`。
