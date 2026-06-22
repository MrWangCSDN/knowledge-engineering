# GitHub 连接 P4c — 可见仓列表 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `GET /scm/connections/{id}/visible-repos` —— 用调用者 user-token 列「我在该连接 SCM 上可见/可绑的仓」+ 标注 KE 已绑状态（onboarding 选仓）。

**Architecture:** 新 dataclass `VisibleRepo`（base.py）；新 helper `resolve_caller_token`（scm_authz.py，取 provider+token，无 principal）；provider 各加 user-token 列仓 `list_user_visible_repos`（GitHub `/user/installations/{id}/repositories` 内联 permissions、GitLab `/projects?membership=true&min_access_level=20`）；端点在 `create_scm_routes` 内（归属校验、Guest-trap、已绑标注、`?q=`）。kill-switch `KE_SCM_VISIBLE_REPOS` 默认关→404。**取数零额外 API、不逐仓核权**。

**Tech Stack:** FastAPI / SQLAlchemy async / httpx / pytest-asyncio / pytest-httpx。

**设计依据:** Obsidian `GitHub仓库连接-P4c-可见仓列表-设计.md`（已过对抗评审，硬化 5I+6M）。

**测试运行约定（必带 env）:**
```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest <路径> -v
```

---

## 文件结构

| 文件 | 责任 | 动作 |
|---|---|---|
| `src/service/scm/base.py` | 新 `VisibleRepo(repo: RepoInfo, role: ScmRole)` dataclass | 改 |
| `src/service/scm/scm_authz.py` | 新 `resolve_caller_token(db, *, user, provider, oauth_cfg, get_login_provider) -> (prov, token)` | 改 |
| `src/service/scm/github_app.py` | 新 `list_user_visible_repos(*, user_token, installation_id)` | 改 |
| `src/service/scm/gitlab_oidc.py` | 新 `list_user_visible_repos(*, user_token)` | 改 |
| `src/service/scm_router.py` | 新端点 `GET /connections/{id}/visible-repos` + 新增 import | 改 |
| `tests/test_auth/test_scm_visible_repos.py` | provider 单测 + 端点矩阵 | 建 |

**关键既有件（照抄，已核对 worktree）：**
- `RepoInfo(external_id:int, full_name:str, default_branch:str, private:bool=False)` / `ScmRole(CAN_BIND/CAN_QUERY/NOT_VISIBLE)` — base.py
- `github_role_to_scm(role_name, permission=None)`：admin/maintain→CAN_BIND，write/triage/read→CAN_QUERY，其余→NOT_VISIBLE — scm_roles.py:13
- `GitHubAppProvider._get_user(user_token, path)`（`token <tok>` 头，**不 raise**，返回原始 resp）、`_is_rate_limited(resp)`（staticmethod，403/429+限流头/body→True）、`list_user_installations`（分页范式：读键 + per_page=100 + `len<100` 终止）— github_app.py
- `GitLabOidcProvider._get_authed(access_token, path)`（`Bearer` 头，**不 raise**，返回原始 resp，base=`{self._cfg.issuer}{path}`）— gitlab_oidc.py
- `resolve_caller_token` 复用：`get_login_provider(provider, oauth_cfg)`（OAuthProviderUnavailable）、`build_refresh_fn(provider, *, gitlab_provider=, oauth_cfg=)`、`get_valid_scm_token(db, *, user_id, provider, refresh_fn)`（ScmTokenInvalid）— 均已在 scm_authz.py import
- `_load_conn(connection_id, user, db)`（404/403）、`flag_on(env_name)`、`Project(scm_connection_id, repo_external_id:BigInteger, id)`、`create_scm_routes` 工厂闭包已有 `oauth_cfg`/`get_login_provider`

---

## Task 1: `VisibleRepo` dataclass + `resolve_caller_token` helper

**Files:**
- Modify: `src/service/scm/base.py`（`VisibleRepo`）
- Modify: `src/service/scm/scm_authz.py`（`resolve_caller_token`）
- Test: `tests/test_auth/test_scm_visible_repos.py`（新建，先放 helper 测试）

- [ ] **Step 1: 写失败测试**

新建 `tests/test_auth/test_scm_visible_repos.py`：
```python
"""P4c 可见仓列表：VisibleRepo / resolve_caller_token / provider 列仓 / 端点矩阵。"""
import pytest
import pytest_asyncio
import httpx
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from src.service.db_models_homepage import Base
from src.service.scm.base import VisibleRepo, RepoInfo, ScmRole
from src.service.scm.scm_authz import resolve_caller_token
from src.service.scm.scm_token_store import upsert_token, ScmTokenInvalid
from src.service.scm.oauth_factory import OAuthProviderUnavailable


class _User:
    id = 1; username = "alice"; gitlab_sub = None


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


def test_visible_repo_dataclass():
    v = VisibleRepo(repo=RepoInfo(external_id=1, full_name="o/r", default_branch="main"), role=ScmRole.CAN_BIND)
    assert v.repo.external_id == 1 and v.role == ScmRole.CAN_BIND


@pytest.mark.asyncio
async def test_resolve_caller_token_happy(maker):
    async with maker() as s:
        await upsert_token(s, user_id=1, provider="github", access_token="AT",
                           refresh_token=None, expires_at=None, scopes=None, scm_login="alice")
        await s.commit()
    sentinel = object()
    async with maker() as s:
        prov, token = await resolve_caller_token(
            s, user=_User(), provider="github", oauth_cfg=object(),
            get_login_provider=lambda p, cfg: sentinel)
    assert prov is sentinel and token == "AT"


@pytest.mark.asyncio
async def test_resolve_caller_token_provider_unavailable_503(maker):
    def _raise(p, cfg):
        raise OAuthProviderUnavailable("x")
    async with maker() as s:
        with pytest.raises(HTTPException) as ei:
            await resolve_caller_token(s, user=_User(), provider="github",
                                       oauth_cfg=object(), get_login_provider=_raise)
    assert ei.value.status_code == 503


@pytest.mark.asyncio
async def test_resolve_caller_token_no_token_403(maker):
    async with maker() as s:                       # 不 seed token → get_valid_scm_token 抛 ScmTokenInvalid
        with pytest.raises(HTTPException) as ei:
            await resolve_caller_token(s, user=_User(), provider="github",
                                       oauth_cfg=object(), get_login_provider=lambda p, cfg: object())
    assert ei.value.status_code == 403
```

- [ ] **Step 2: 跑红**

```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth/test_scm_visible_repos.py -v
```
预期：FAIL（`ImportError: cannot import name 'VisibleRepo'` / `resolve_caller_token`）。

- [ ] **Step 3: 实现**

在 `src/service/scm/base.py` 的 `RepoInfo` 之后加：
```python
@dataclass(frozen=True)
class VisibleRepo:
    repo: RepoInfo
    role: ScmRole        # CAN_BIND / CAN_QUERY（NOT_VISIBLE 在 provider 内已滤）
```

在 `src/service/scm/scm_authz.py` 末尾（`create_authorize_scm` 之外、模块级）加：
```python
async def resolve_caller_token(db, *, user, provider, oauth_cfg, get_login_provider):
    """造 provider + 取调用者明文 user-token。异常→HTTPException(503/403/502)。
    不解析 principal（P4c 列仓走 user 身份本身，无需 collaborator principal）。"""
    try:
        prov = get_login_provider(provider, oauth_cfg)
    except OAuthProviderUnavailable:
        raise HTTPException(status_code=503, detail=f"{provider} 未配置")
    refresh_fn = build_refresh_fn(
        provider,
        gitlab_provider=prov if provider == "gitlab" else None,
        oauth_cfg=oauth_cfg)
    try:
        token = await get_valid_scm_token(db, user_id=user.id, provider=provider, refresh_fn=refresh_fn)
    except ScmTokenInvalid:
        raise HTTPException(status_code=403, detail="请先关联 SCM 账号")
    except (httpx.HTTPError, RuntimeError):
        raise HTTPException(status_code=502, detail="SCM 授权刷新失败，请重试")
    return prov, token
```
> scm_authz.py 顶部已 import `httpx`/`HTTPException`/`build_refresh_fn`/`get_valid_scm_token`/`ScmTokenInvalid`/`OAuthProviderUnavailable`——无需新增 import。

- [ ] **Step 4: 跑绿**

```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth/test_scm_visible_repos.py -v
```
预期：4 passed。

- [ ] **Step 5: Commit**

```bash
git add src/service/scm/base.py src/service/scm/scm_authz.py tests/test_auth/test_scm_visible_repos.py
git commit -m "feat(scm): VisibleRepo + resolve_caller_token helper（P4c T1）"
```

---

## Task 2: GitHub `list_user_visible_repos`（user-token，内联 permissions）

**Files:**
- Modify: `src/service/scm/github_app.py`
- Test: `tests/test_auth/test_scm_visible_repos.py`（追加 GitHub provider 单测）

- [ ] **Step 1: 写失败测试**（追加到测试文件）

```python
_API = "https://api.github.com"


def _gh_provider():
    from src.service.scm.github_app import GitHubAppProvider
    from src.service.scm.config import GitHubAppConfig
    return GitHubAppProvider(GitHubAppConfig(app_id="1", private_key_pem="x", webhook_secret=""))


def _repo(rid, name, perms, default_branch="main", private=True):
    return {"id": rid, "full_name": name, "default_branch": default_branch,
            "private": private, "permissions": perms}


@pytest.mark.asyncio
async def test_gh_visible_maps_roles_and_filters(httpx_mock):
    url = f"{_API}/user/installations/55/repositories?per_page=100&page=1"
    httpx_mock.add_response(url=url, json={"repositories": [
        _repo(1, "o/admin", {"admin": True, "push": True, "pull": True}),
        _repo(2, "o/push", {"admin": False, "maintain": False, "push": True, "pull": True}),
        _repo(3, "o/none", {"admin": False, "maintain": False, "push": False, "triage": False, "pull": False}),
    ]})
    out = await _gh_provider().list_user_visible_repos(user_token="UT", installation_id=55)
    by = {v.repo.full_name: v.role for v in out}
    assert by == {"o/admin": ScmRole.CAN_BIND, "o/push": ScmRole.CAN_QUERY}   # o/none 被滤除
    assert all(req.headers["Authorization"] == "token UT" for req in httpx_mock.get_requests())


@pytest.mark.asyncio
async def test_gh_visible_pagination(httpx_mock):
    page1 = {"repositories": [_repo(i, f"o/r{i}", {"pull": True}) for i in range(100)]}
    page2 = {"repositories": [_repo(100, "o/last", {"admin": True})]}
    httpx_mock.add_response(url=f"{_API}/user/installations/55/repositories?per_page=100&page=1", json=page1)
    httpx_mock.add_response(url=f"{_API}/user/installations/55/repositories?per_page=100&page=2", json=page2)
    out = await _gh_provider().list_user_visible_repos(user_token="UT", installation_id=55)
    assert len(out) == 101 and out[-1].repo.full_name == "o/last"


@pytest.mark.asyncio
async def test_gh_visible_no_install_access_403_returns_empty(httpx_mock):
    httpx_mock.add_response(url=f"{_API}/user/installations/55/repositories?per_page=100&page=1",
                            status_code=404)
    out = await _gh_provider().list_user_visible_repos(user_token="UT", installation_id=55)
    assert out == []


@pytest.mark.asyncio
async def test_gh_visible_rate_limited_raises(httpx_mock):
    httpx_mock.add_response(url=f"{_API}/user/installations/55/repositories?per_page=100&page=1",
                            status_code=403, headers={"x-ratelimit-remaining": "0"})
    with pytest.raises(httpx.HTTPError):
        await _gh_provider().list_user_visible_repos(user_token="UT", installation_id=55)
```

- [ ] **Step 2: 跑红**

```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth/test_scm_visible_repos.py -k gh_visible -v
```
预期：FAIL（`AttributeError: ... has no attribute 'list_user_visible_repos'`）。

- [ ] **Step 3: 实现**

在 `src/service/scm/github_app.py` 的 `list_user_installations` 之后加：
```python
    async def list_user_visible_repos(self, *, user_token: str, installation_id: int) -> list["VisibleRepo"]:
        """该用户在指定 installation 下可见仓（user-token 视角，GET /user/installations/{id}/repositories）。
        内联 permissions 推 role_name → github_role_to_scm；NOT_VISIBLE 滤除。
        非限流 403/404（无安装访问）→ 空列表；限流 403/429 或 5xx → raise（上层 502）。"""
        from src.service.scm.base import RepoInfo, VisibleRepo, ScmRole
        from src.service.scm.scm_roles import github_role_to_scm
        out: list[VisibleRepo] = []
        page = 1
        while True:
            resp = await self._get_user(
                user_token, f"/user/installations/{installation_id}/repositories?per_page=100&page={page}")
            if resp.status_code in (403, 404) and not self._is_rate_limited(resp):
                return []                                   # 调用者对该安装无访问 → 合法空集（fail-closed）
            resp.raise_for_status()                          # 限流 403/429 或 5xx → HTTPStatusError → 上层 502
            repos = resp.json().get("repositories", [])
            if not repos:
                break
            for r in repos:
                perms = r.get("permissions") or {}
                role_name = ("admin" if perms.get("admin") else
                             "maintain" if perms.get("maintain") else
                             "write" if perms.get("push") else
                             "triage" if perms.get("triage") else
                             "read" if perms.get("pull") else None)
                role = github_role_to_scm(role_name)
                if role == ScmRole.NOT_VISIBLE:
                    continue
                out.append(VisibleRepo(
                    repo=RepoInfo(external_id=r["id"], full_name=r["full_name"],
                                  default_branch=r.get("default_branch") or "main",
                                  private=bool(r.get("private"))),
                    role=role))
            if len(repos) < 100:
                break
            page += 1
        return out
```

- [ ] **Step 4: 跑绿**

```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth/test_scm_visible_repos.py -k "gh_visible or visible_repo or resolve_caller" -v
```
预期：全绿。

- [ ] **Step 5: Commit**

```bash
git add src/service/scm/github_app.py tests/test_auth/test_scm_visible_repos.py
git commit -m "feat(scm): GitHub list_user_visible_repos（user-token 内联权限+403/404 空集）（P4c T2）"
```

---

## Task 3: GitLab `list_user_visible_repos`（server-floor + permissions 升档）

**Files:**
- Modify: `src/service/scm/gitlab_oidc.py`
- Test: `tests/test_auth/test_scm_visible_repos.py`（追加 GitLab provider 单测）

- [ ] **Step 1: 写失败测试**（追加）

```python
_ISS = "https://gitlab.example.com"


def _gl_provider():
    from src.service.scm.gitlab_oidc import GitLabOidcProvider
    from src.service.scm.config import GitLabOidcConfig
    return GitLabOidcProvider(GitLabOidcConfig(issuer=_ISS, client_id="c", client_secret="s"))


def _proj(pid, name, perms, visibility="private", default_branch="main"):
    return {"id": pid, "path_with_namespace": name, "default_branch": default_branch,
            "visibility": visibility, "permissions": perms}


@pytest.mark.asyncio
async def test_gl_visible_roles_and_inherited_floor(httpx_mock):
    url = f"{_ISS}/api/v4/projects?membership=true&min_access_level=20&per_page=100&page=1"
    httpx_mock.add_response(url=url, json=[
        _proj(1, "g/maint", {"project_access": {"access_level": 40}, "group_access": None}),
        _proj(2, "g/report", {"project_access": {"access_level": 20}, "group_access": None}),
        _proj(3, "g/groupowner", {"project_access": None, "group_access": {"access_level": 50}}),
        _proj(4, "g/inherited", {"project_access": None, "group_access": None}, visibility="public"),
    ])
    out = await _gl_provider().list_user_visible_repos(user_token="AT")
    by = {v.repo.full_name: v.role for v in out}
    assert by == {
        "g/maint": ScmRole.CAN_BIND, "g/report": ScmRole.CAN_QUERY,
        "g/groupowner": ScmRole.CAN_BIND,
        "g/inherited": ScmRole.CAN_QUERY,        # 双 null（纯继承）→ 入选下限 can_query，不被误滤
    }
    assert by["g/inherited"] == ScmRole.CAN_QUERY
    # private 推导
    pri = {v.repo.full_name: v.repo.private for v in out}
    assert pri["g/inherited"] is False and pri["g/maint"] is True
    assert all(req.headers["Authorization"] == "Bearer AT" for req in httpx_mock.get_requests())


@pytest.mark.asyncio
async def test_gl_visible_pagination(httpx_mock):
    page1 = [_proj(i, f"g/r{i}", {"project_access": {"access_level": 20}}) for i in range(100)]
    page2 = [_proj(100, "g/last", {"project_access": {"access_level": 40}})]
    httpx_mock.add_response(
        url=f"{_ISS}/api/v4/projects?membership=true&min_access_level=20&per_page=100&page=1", json=page1)
    httpx_mock.add_response(
        url=f"{_ISS}/api/v4/projects?membership=true&min_access_level=20&per_page=100&page=2", json=page2)
    out = await _gl_provider().list_user_visible_repos(user_token="AT")
    assert len(out) == 101 and out[-1].repo.full_name == "g/last" and out[-1].role == ScmRole.CAN_BIND
```

- [ ] **Step 2: 跑红**

```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth/test_scm_visible_repos.py -k gl_visible -v
```
预期：FAIL（无 `list_user_visible_repos`）。

- [ ] **Step 3: 实现**

在 `src/service/scm/gitlab_oidc.py` 的 `resolve_repo_role` 之后加：
```python
    async def list_user_visible_repos(self, *, user_token: str) -> list["VisibleRepo"]:
        """用户可见 GitLab project（GET /projects?membership=true&min_access_level=20，Bearer，分页）。
        服务端 min_access_level=20 是可见性权威下限：入选即 ≥CAN_QUERY；permissions 仅用于升档 CAN_BIND
        （permissions.{project_access,group_access} 在纯继承访问时可能皆 null，绝不据此判 NOT_VISIBLE）。"""
        from src.service.scm.base import RepoInfo, VisibleRepo, ScmRole
        out: list[VisibleRepo] = []
        page = 1
        while True:
            resp = await self._get_authed(
                user_token, f"/api/v4/projects?membership=true&min_access_level=20&per_page=100&page={page}")
            resp.raise_for_status()                          # 401/5xx → HTTPStatusError → 上层 502
            projects = resp.json()
            if not projects:
                break
            for p in projects:
                perms = p.get("permissions") or {}
                levels = []
                pa, ga = perms.get("project_access"), perms.get("group_access")
                if pa and pa.get("access_level") is not None:
                    levels.append(int(pa["access_level"]))
                if ga and ga.get("access_level") is not None:
                    levels.append(int(ga["access_level"]))
                level = max(levels) if levels else None
                role = ScmRole.CAN_BIND if (level is not None and level >= 40) else ScmRole.CAN_QUERY
                out.append(VisibleRepo(
                    repo=RepoInfo(external_id=p["id"], full_name=p["path_with_namespace"],
                                  default_branch=p.get("default_branch") or "main",
                                  private=(p.get("visibility") != "public")),
                    role=role))
            if len(projects) < 100:
                break
            page += 1
        return out
```

- [ ] **Step 4: 跑绿**

```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth/test_scm_visible_repos.py -k gl_visible -v
```
预期：2 passed。

- [ ] **Step 5: Commit**

```bash
git add src/service/scm/gitlab_oidc.py tests/test_auth/test_scm_visible_repos.py
git commit -m "feat(scm): GitLab list_user_visible_repos（server-floor+permissions 升档，双null不误滤）（P4c T3）"
```

---

## Task 4: 端点 `GET /connections/{id}/visible-repos` + 装配 + 全回归

**Files:**
- Modify: `src/service/scm_router.py`（新增 import + 端点）
- Test: `tests/test_auth/test_scm_visible_repos.py`（端点矩阵）

- [ ] **Step 1: 写失败测试**（追加端点矩阵）

```python
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from src.service.scm_router import create_scm_routes
from src.service.db_models_homepage import ScmConnection, Project


class _FakeVisProvider:
    def __init__(self, items): self._items = items
    async def list_user_visible_repos(self, *, user_token, installation_id=None):
        return self._items


def _vis_app(maker, *, conn, items=None, user=None, get_login_provider=None):
    app = FastAPI()
    prov = _FakeVisProvider(items or [])
    async def _get_db():
        async with maker() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise
    app.include_router(create_scm_routes(
        get_current_user=lambda: (user or _User()), get_db=_get_db,
        get_provider=lambda: prov, app_slug="ke-test-app",
        oauth_cfg=object(), get_login_provider=get_login_provider or (lambda p, cfg: prov)))
    return app


async def _seed_conn(maker, *, cid="c1", provider="github", auth_type="github_app",
                     installation_id=55, created_by="alice"):
    async with maker() as s:
        s.add(ScmConnection(id=cid, provider=provider, auth_type=auth_type,
                            github_installation_id=installation_id, account_login="o",
                            status="active", created_by=created_by))
        await s.commit()


async def _seed_user_token(maker):
    async with maker() as s:
        await upsert_token(s, user_id=1, provider="github", access_token="AT",
                           refresh_token=None, expires_at=None, scopes=None, scm_login="alice")
        await s.commit()


def _vr(rid, name, role=ScmRole.CAN_BIND):
    return VisibleRepo(repo=RepoInfo(external_id=rid, full_name=name, default_branch="main", private=True), role=role)


@pytest.mark.asyncio
async def test_endpoint_flag_off_404(maker, monkeypatch):
    monkeypatch.delenv("KE_SCM_VISIBLE_REPOS", raising=False)
    await _seed_conn(maker); await _seed_user_token(maker)
    c = TestClient(_vis_app(maker, conn="c1", items=[_vr(1, "o/r")]))
    assert c.get("/scm/connections/c1/visible-repos").status_code == 404


@pytest.mark.asyncio
async def test_endpoint_happy_with_bound_and_q(maker, monkeypatch):
    monkeypatch.setenv("KE_SCM_VISIBLE_REPOS", "1")
    await _seed_conn(maker); await _seed_user_token(maker)
    async with maker() as s:                          # 仓 1 已绑到 project p1
        s.add(Project(id="p1", name="P1", scm_connection_id="c1", repo_external_id=1))
        await s.commit()
    items = [_vr(1, "org/alpha", ScmRole.CAN_BIND), _vr(2, "org/beta", ScmRole.CAN_QUERY)]
    c = TestClient(_vis_app(maker, conn="c1", items=items))
    r = c.get("/scm/connections/c1/visible-repos")
    assert r.status_code == 200
    repos = {x["full_name"]: x for x in r.json()["repos"]}
    assert repos["org/alpha"]["scm_role"] == "can_bind"
    assert repos["org/alpha"]["bound"] is True and repos["org/alpha"]["bound_project_id"] == "p1"
    assert repos["org/beta"]["scm_role"] == "can_query" and repos["org/beta"]["bound"] is False
    # q 过滤
    r2 = c.get("/scm/connections/c1/visible-repos", params={"q": "beta"})
    assert [x["full_name"] for x in r2.json()["repos"]] == ["org/beta"]


@pytest.mark.asyncio
async def test_endpoint_not_found_and_forbidden(maker, monkeypatch):
    monkeypatch.setenv("KE_SCM_VISIBLE_REPOS", "1")
    await _seed_user_token(maker)
    c = TestClient(_vis_app(maker, conn="c1", items=[]))
    assert c.get("/scm/connections/missing/visible-repos").status_code == 404   # 连接不存在
    await _seed_conn(maker, cid="c2", created_by="bob")                          # 别人的连接
    assert c.get("/scm/connections/c2/visible-repos").status_code == 403


@pytest.mark.asyncio
async def test_endpoint_pat_and_bad_provider_422(maker, monkeypatch):
    monkeypatch.setenv("KE_SCM_VISIBLE_REPOS", "1")
    await _seed_user_token(maker)
    await _seed_conn(maker, cid="cpat", auth_type="pat", installation_id=None)
    c = TestClient(_vis_app(maker, conn="cpat", items=[]))
    assert c.get("/scm/connections/cpat/visible-repos").status_code == 422
    await _seed_conn(maker, cid="cbad", provider="bitbucket")
    assert c.get("/scm/connections/cbad/visible-repos").status_code == 422


@pytest.mark.asyncio
async def test_endpoint_github_installation_none_422(maker, monkeypatch):
    monkeypatch.setenv("KE_SCM_VISIBLE_REPOS", "1")
    await _seed_user_token(maker)
    await _seed_conn(maker, cid="cnull", auth_type="github_app", installation_id=None)
    c = TestClient(_vis_app(maker, conn="cnull", items=[]))
    assert c.get("/scm/connections/cnull/visible-repos").status_code == 422


@pytest.mark.asyncio
async def test_endpoint_provider_unavailable_503(maker, monkeypatch):
    monkeypatch.setenv("KE_SCM_VISIBLE_REPOS", "1")
    await _seed_conn(maker); await _seed_user_token(maker)

    def _raise(p, cfg):
        raise OAuthProviderUnavailable("x")
    c = TestClient(_vis_app(maker, conn="c1", items=[], get_login_provider=_raise))
    assert c.get("/scm/connections/c1/visible-repos").status_code == 503


@pytest.mark.asyncio
async def test_endpoint_empty_set_200(maker, monkeypatch):
    monkeypatch.setenv("KE_SCM_VISIBLE_REPOS", "1")
    await _seed_conn(maker); await _seed_user_token(maker)
    c = TestClient(_vis_app(maker, conn="c1", items=[]))   # provider 返回 [] (模拟无安装访问)
    r = c.get("/scm/connections/c1/visible-repos")
    assert r.status_code == 200 and r.json() == {"repos": []}
```

- [ ] **Step 2: 跑红**

```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth/test_scm_visible_repos.py -k endpoint -v
```
预期：FAIL（端点 404 = 路由不存在；但 test_flag_off_404 可能假绿——FastAPI 对未注册路由也返 404。**实现后**所有 endpoint 用例才真正区分）。

- [ ] **Step 3: 实现**

`src/service/scm_router.py` 顶部 import 区**新增/修改**：
```python
from src.service.db_models_homepage import ScmConnection, UserScmToken, Project   # 加 Project
from src.service.scm.scm_authz import flag_on, resolve_caller_token               # 新增
from src.service.scm.base import ScmRole                                          # 新增
```
（`select`/`httpx`/`HTTPException`/`Depends` 已 import。）

在 `create_scm_routes` 内（`list_repos` 端点附近）加：
```python
    @router.get("/connections/{connection_id}/visible-repos")
    async def visible_repos(connection_id: str, q: str = "",
                            user=Depends(get_current_user), db=Depends(get_db)) -> dict:
        """列调用者在该连接 SCM 上可见/可绑的仓 + KE 已绑标注（onboarding 选仓）。kill-switch 默认关。"""
        if not flag_on("KE_SCM_VISIBLE_REPOS"):
            raise HTTPException(status_code=404, detail="not found")
        conn = await _load_conn(connection_id, user, db)            # 归属（404/403）
        if conn.auth_type == "pat":
            raise HTTPException(status_code=422, detail="PAT 连接不支持可见仓列举")
        if conn.provider not in ("github", "gitlab"):
            raise HTTPException(status_code=422, detail="不支持的 provider")
        prov, token = await resolve_caller_token(
            db, user=user, provider=conn.provider, oauth_cfg=oauth_cfg, get_login_provider=get_login_provider)
        try:
            if conn.provider == "github":
                if conn.github_installation_id is None:             # 与 /repos 守卫一致
                    raise HTTPException(status_code=422, detail="该连接不是 GitHub App 类型")
                visibles = await prov.list_user_visible_repos(
                    user_token=token, installation_id=conn.github_installation_id)
            else:
                visibles = await prov.list_user_visible_repos(user_token=token)
        except httpx.HTTPError:                                     # 仅限流/5xx 到这（非限流 403/404 已在 provider→[]）
            raise HTTPException(status_code=502, detail="列举可见仓失败，请重试")
        ext_ids = [v.repo.external_id for v in visibles]
        bound = {}
        if ext_ids:
            rows = (await db.execute(select(Project.id, Project.repo_external_id).where(
                Project.scm_connection_id == conn.id, Project.repo_external_id.in_(ext_ids)))).all()
            bound = {r.repo_external_id: r.id for r in rows}
        ql = q.strip().lower()
        out = []
        for v in visibles:
            if ql and ql not in v.repo.full_name.lower():
                continue
            out.append({
                "external_id": v.repo.external_id, "full_name": v.repo.full_name,
                "default_branch": v.repo.default_branch, "private": v.repo.private,
                "scm_role": "can_bind" if v.role == ScmRole.CAN_BIND else "can_query",
                "bound": v.repo.external_id in bound,
                "bound_project_id": bound.get(v.repo.external_id),
            })
        return {"repos": out}
```
> 注：`HTTPException(422)` 在 try 内被 `except httpx.HTTPError` 漏过（HTTPException 不是 httpx.HTTPError），正确冒泡为 422。

- [ ] **Step 4: 跑绿**

```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth/test_scm_visible_repos.py -v
```
预期：全文件全绿。

- [ ] **Step 5: import 冒烟 + 全量回归**

```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -c "import src.service.api; print('import ok')"
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth -q
```
预期：import ok；全量绿（P4b-2 基线 1069 + 本片新增用例）。

- [ ] **Step 6: Commit**

```bash
git add src/service/scm_router.py tests/test_auth/test_scm_visible_repos.py
git commit -m "feat(scm): GET /connections/{id}/visible-repos 端点（归属+Guest-trap+已绑标注，flag 默认关）（P4c T4）"
```

---

## 验收标准（P4c Done）

1. **flag**：关→404；开后端点工作。
2. **归属**：不存在→404、非归属→403、PAT→422、非法 provider→422、github_installation_id=None→422。
3. **GitHub**：user-token 列仓、permissions 映射（admin/maintain→can_bind、write/triage/read→can_query、无权滤除）、403/404→空集、限流/5xx→502、分页拼接。
4. **GitLab（fake-provider 单测）**：入选即 ≥can_query、permissions max≥40 升档 can_bind、双 null 不误滤、分页拼接、private 由 visibility 推导。
5. **响应**：含 scm_role/bound/bound_project_id；`?q=` 过滤；空集→`{"repos": []}`。
6. **回归**：全量 `tests/test_auth` 绿 + `import src.service.api` 冒烟。

## 自审记录（writing-plans self-review）

- **Spec 覆盖**：VisibleRepo+resolve_caller_token→T1；GitHub 列仓→T2；GitLab 列仓→T3；端点+装配→T4；§6 全部用例（含 GitHub 403/404→空集、GitLab 双 null→can_query、已绑标注、q、503/422 矩阵）映射到 T1-T4。✅
- **类型一致**：`_get_user`（不 raise，返回 raw resp）、`_is_rate_limited`（staticmethod）、`_get_authed`（Bearer，不 raise）、`github_role_to_scm`（role_name 映射）、`RepoInfo`/`ScmRole`/`VisibleRepo` 字段、`Project.repo_external_id`、`flag_on`、`resolve_caller_token` 签名均按 worktree 实际核对。✅
- **占位符**：无 TBD。✅
- **风险点**：(a) GitHub 403/404→[] 必须在 raise_for_status 前判（已置于循环顶）；(b) GitLab 双 null 绝不判 NOT_VISIBLE（入选即 can_query）；(c) 端点 try 只 catch httpx.HTTPError，HTTPException(422) 正确冒泡；(d) GitLab 端点分支生产暂不可达（无连接创建路径），仅 fake-provider 单测覆盖（见 spec §1/§9）。
