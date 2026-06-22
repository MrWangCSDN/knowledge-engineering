# GitHub 连接 P4b-0：SCM 权限解析 provider 基座 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL：superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans，逐任务执行。Steps 用 `- [ ]`。

**Goal:** 造「用户 user-to-server token + 仓库 → `ScmRole`」的纯 provider 能力 + token 刷新 + 正向缓存，作为 P4b-1（bind/QA 门）、P4b-2（callback 归属）的共享基座。

**Architecture:** 纯库，**不改任何 FastAPI 路由**。GitHub/GitLab 各加 `resolve_repo_role`（显式 user token、状态矩阵 fail-closed、GitHub 只读 403 自查回退）；`scm_roles` 纯映射；`scm_refresh.build_refresh_fn`（async 闭包，GitLab 经 discovery 取 token 端点、`expires_in`→`expires_at` datetime、按 body `error` 判 invalid_grant）；`scm_perm_cache` 模块级 TTL 缓存（仅正向）。调用方按 `conn.provider` 分派（不加 Protocol 强约束）。

**Tech Stack:** httpx / pytest + pytest-httpx。复用 P4a 的 `get_valid_scm_token`/`ScmTokenInvalid`/`RefreshFn`、`GitHubAppProvider`/`GitLabOidcProvider`、`ScmRole`、`OAuthConfig`。

**设计依据:** [[GitHub仓库连接-P4b-0-SCM权限解析基座-设计]]（spec，过对抗评审）；[[身份与授权模型-设计]]。

**前置:** P1+P2+P3+P4a 已在本分支。worktree `/Users/java/ke-github-connect`，分支 `feat/github-repo-connect`。测试统一前缀：
```
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest <path> -v
```

**关键既有 API（勿臆造）:**
- `src/service/scm/base.py`：`ScmRole(str, Enum)` 值 `CAN_BIND="can_bind"`/`CAN_QUERY="can_query"`/`NOT_VISIBLE="not_visible"`。
- `src/service/scm/github_app.py`：`_API="https://api.github.com"`；user-token header 形态（见 `get_login_identity`）= `{"Authorization": f"token {tok}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}`；`_get(installation_id, path)` 用的是 **installation token（P4b-0 禁用此路径）**；`list_repos` 分页范式（读 `.get("repositories", [])`，per_page=100）。
- `src/service/scm/gitlab_oidc.py`：`_discovery()`(async，缓存，返回含 `token_endpoint`/`jwks_uri` 的 dict)；`self._cfg`=`GitLabOidcConfig(issuer, client_id, client_secret)`。
- `src/service/scm/scm_token_store.py`：`RefreshFn = Callable[[str], Awaitable[dict]]`；`class ScmTokenInvalid(Exception)`；刷新 dict 契约 = `{access_token:str, refresh_token:Optional[str], expires_at:Optional[datetime]}`。
- `src/service/scm/config.py`：`OAuthConfig.github`=`GitHubOAuthConfig(client_id, client_secret)`；`.gitlab`=`GitLabOidcConfig`。

**范围边界:** 不改路由、不接 `authorize()`、不做 callback 归属、不做 membership 缓存（P4c）、不加 ScmProvider Protocol 强约束（call-site dispatch）。仅 GitHub.com（GHES 不支持）。

---

## 文件结构

| 文件 | 责任 |
|---|---|
| `src/service/scm/scm_roles.py`（新） | `github_role_to_scm` / `gitlab_access_level_to_scm` 纯映射 |
| `src/service/scm/github_app.py`（改） | `_get_user` + `_is_rate_limited` + `list_user_installations` + `resolve_repo_role`(+只读回退) |
| `src/service/scm/gitlab_oidc.py`（改） | `_get_authed` + `resolve_repo_role`(Members API + sub 守卫) |
| `src/service/scm/scm_refresh.py`（新） | `build_refresh_fn`(async 闭包) |
| `src/service/scm/scm_perm_cache.py`（新） | 模块级正向 TTL 缓存 + `resolve_repo_role_cached` + `cache_invalidate` + `cache_clear` |
| `tests/test_auth/test_scm_roles.py` / `test_github_repo_role.py` / `test_gitlab_repo_role.py` / `test_scm_refresh.py` / `test_scm_perm_cache.py` | 测试 |

---

## Task 1：角色映射纯函数

**Files:** Create `src/service/scm/scm_roles.py`；Test `tests/test_auth/test_scm_roles.py`

- [ ] **Step 1: 失败测试**
```python
# tests/test_auth/test_scm_roles.py
import pytest
from src.service.scm.base import ScmRole
from src.service.scm.scm_roles import github_role_to_scm, gitlab_access_level_to_scm


@pytest.mark.parametrize("role_name,expected", [
    ("admin", ScmRole.CAN_BIND), ("maintain", ScmRole.CAN_BIND),
    ("write", ScmRole.CAN_QUERY), ("triage", ScmRole.CAN_QUERY), ("read", ScmRole.CAN_QUERY),
    ("none", ScmRole.NOT_VISIBLE), ("", ScmRole.NOT_VISIBLE), (None, ScmRole.NOT_VISIBLE),
    ("ADMIN", ScmRole.CAN_BIND),  # 大小写不敏感
])
def test_github_role_name(role_name, expected):
    assert github_role_to_scm(role_name) == expected


def test_github_custom_role_falls_back_to_permission():
    # 自定义 org 角色（非内建）→ 回退 legacy permission 字段
    assert github_role_to_scm("custom-org-role", "read") == ScmRole.CAN_QUERY
    assert github_role_to_scm("custom-org-role", "admin") == ScmRole.CAN_BIND
    assert github_role_to_scm("custom-org-role", "none") == ScmRole.NOT_VISIBLE
    assert github_role_to_scm("custom-org-role", None) == ScmRole.NOT_VISIBLE


@pytest.mark.parametrize("level,expected", [
    (50, ScmRole.CAN_BIND), (40, ScmRole.CAN_BIND),
    (30, ScmRole.CAN_QUERY), (20, ScmRole.CAN_QUERY),
    (10, ScmRole.NOT_VISIBLE), (0, ScmRole.NOT_VISIBLE),  # Guest=10 → not_visible（Guest-trap）
])
def test_gitlab_access_level(level, expected):
    assert gitlab_access_level_to_scm(level) == expected
```

- [ ] **Step 2: 跑测试确认失败**（ImportError）。

- [ ] **Step 3: 实现**
```python
# src/service/scm/scm_roles.py
"""SCM 原生角色 → 内部 ScmRole 三档映射（纯函数）。设计 §4.1。"""
from __future__ import annotations

from typing import Optional

from src.service.scm.base import ScmRole

_GH_BIND = {"admin", "maintain"}
_GH_QUERY = {"write", "triage", "read"}


def github_role_to_scm(role_name: Optional[str], permission: Optional[str] = None) -> ScmRole:
    """GitHub collaborators permission API：优先内建 role_name；自定义 org 角色回退 legacy permission。"""
    rn = (role_name or "").lower()
    if rn in _GH_BIND:
        return ScmRole.CAN_BIND
    if rn in _GH_QUERY:
        return ScmRole.CAN_QUERY
    # role_name 可能是自定义 org 角色（非内建 5 值）→ 回退稳定的 legacy permission(admin/write/read/none)
    pm = (permission or "").lower()
    if pm == "admin":
        return ScmRole.CAN_BIND
    if pm in ("write", "read"):
        return ScmRole.CAN_QUERY
    return ScmRole.NOT_VISIBLE


def gitlab_access_level_to_scm(level: int) -> ScmRole:
    """GitLab access_level：50 Owner/40 Maintainer/30 Developer/20 Reporter/10 Guest。"""
    if level >= 40:
        return ScmRole.CAN_BIND
    if level >= 20:
        return ScmRole.CAN_QUERY   # Guest=10 < 20 → NOT_VISIBLE（Guest-trap）
    return ScmRole.NOT_VISIBLE
```

- [ ] **Step 4: 跑测试确认通过**（全 parametrize + 2 = ~14 passed）。

- [ ] **Step 5: 提交**
```bash
git add src/service/scm/scm_roles.py tests/test_auth/test_scm_roles.py && git commit -m "feat(scm): SCM 原生角色→ScmRole 映射（role_name+permission 回退 / access_level Guest-trap）"
```

---

## Task 2：GitHubAppProvider 仓权限解析（user token + 状态矩阵 + 只读回退）

**Files:** Modify `src/service/scm/github_app.py`；Test `tests/test_auth/test_github_repo_role.py`

> **A7 不变量**：新 `_get_user` 只用 user token，绝不调 `get_installation_token`/`_get`。`user_token` 关键字参数。

- [ ] **Step 1: 失败测试**（pytest-httpx mock）
```python
# tests/test_auth/test_github_repo_role.py
import pytest
from src.service.scm.github_app import GitHubAppProvider
from src.service.scm.config import GitHubAppConfig
from src.service.scm.base import ScmRole

_API = "https://api.github.com"


def _provider():
    return GitHubAppProvider(GitHubAppConfig(app_id="1", private_key_pem="x", webhook_secret=""))


@pytest.mark.asyncio
async def test_admin_can_bind(httpx_mock):
    httpx_mock.add_response(url=f"{_API}/repos/o/r/collaborators/octocat/permission",
                            json={"role_name": "admin", "permission": "admin"})
    p = _provider()
    assert await p.resolve_repo_role(token="UT", repo="o/r", principal="octocat") == ScmRole.CAN_BIND


@pytest.mark.asyncio
async def test_uses_user_token_not_installation(httpx_mock, monkeypatch):
    # A7：断言用 user token header，且 get_installation_token 从未被调用
    called = {"inst": False}
    async def _boom(self, installation_id):
        called["inst"] = True
        return "INST"
    monkeypatch.setattr(GitHubAppProvider, "get_installation_token", _boom)
    httpx_mock.add_response(url=f"{_API}/repos/o/r/collaborators/u/permission",
                            json={"role_name": "read", "permission": "read"})
    p = _provider()
    role = await p.resolve_repo_role(token="USER_TOKEN_123", repo="o/r", principal="u")
    assert role == ScmRole.CAN_QUERY
    assert called["inst"] is False
    req = httpx_mock.get_requests()[0]
    assert req.headers["Authorization"] == "token USER_TOKEN_123"


@pytest.mark.asyncio
async def test_scm_login_none_not_visible(httpx_mock):
    p = _provider()
    # principal None → 不发请求、直接 NOT_VISIBLE
    assert await p.resolve_repo_role(token="UT", repo="o/r", principal=None) == ScmRole.NOT_VISIBLE
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_401_not_visible_not_502(httpx_mock):
    httpx_mock.add_response(url=f"{_API}/repos/o/r/collaborators/u/permission", status_code=401)
    p = _provider()
    assert await p.resolve_repo_role(token="UT", repo="o/r", principal="u") == ScmRole.NOT_VISIBLE


@pytest.mark.asyncio
async def test_404_then_repo_fallback_not_visible(httpx_mock):
    httpx_mock.add_response(url=f"{_API}/repos/o/r/collaborators/u/permission", status_code=404)
    httpx_mock.add_response(url=f"{_API}/repos/o/r", status_code=404)
    p = _provider()
    assert await p.resolve_repo_role(token="UT", repo="o/r", principal="u") == ScmRole.NOT_VISIBLE


@pytest.mark.asyncio
async def test_readonly_403_fallback_can_query(httpx_mock):
    # permission 端点对只读用户 403 → 回退 GET /repos 200 → CAN_QUERY
    httpx_mock.add_response(url=f"{_API}/repos/o/r/collaborators/u/permission", status_code=403)
    httpx_mock.add_response(url=f"{_API}/repos/o/r", status_code=200, json={"id": 1})
    p = _provider()
    assert await p.resolve_repo_role(token="UT", repo="o/r", principal="u") == ScmRole.CAN_QUERY


@pytest.mark.asyncio
async def test_rate_limited_403_raises(httpx_mock):
    httpx_mock.add_response(url=f"{_API}/repos/o/r/collaborators/u/permission",
                            status_code=403, headers={"x-ratelimit-remaining": "0"}, json={"message": "rate limit"})
    p = _provider()
    with pytest.raises(Exception):   # 限流 → 抛（上层 502）
        await p.resolve_repo_role(token="UT", repo="o/r", principal="u")


@pytest.mark.asyncio
async def test_list_user_installations_paginated(httpx_mock):
    page1 = {"installations": [{"id": i} for i in range(100)]}
    page2 = {"installations": [{"id": 999}]}
    httpx_mock.add_response(url=f"{_API}/user/installations?per_page=100&page=1", json=page1)
    httpx_mock.add_response(url=f"{_API}/user/installations?per_page=100&page=2", json=page2)
    p = _provider()
    ids = await p.list_user_installations(user_token="UT")
    assert 999 in ids and len(ids) == 101
    assert httpx_mock.get_requests()[0].headers["Authorization"] == "token UT"
```

- [ ] **Step 2: 跑测试确认失败**（应在收集/导入期就红："为正确原因失败"——新模块未建→`ImportError`；已有类未加方法→`AttributeError: ... 'resolve_repo_role'`）。

- [ ] **Step 3: 实现**（追加到 `GitHubAppProvider` 类内，放在 `get_login_identity` 之后；顶部已 import `httpx`、有 `_API`、`Optional`）
```python
    async def _get_user(self, user_token: str, path: str) -> httpx.Response:
        """用 user-to-server token 发 GET。A7：绝不用 installation/app token。返回原始 Response（调用方按 status 分支）。"""
        headers = {
            "Authorization": f"token {user_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            return await client.get(f"{_API}{path}", headers=headers)

    @staticmethod
    def _is_rate_limited(resp: httpx.Response) -> bool:
        """区分"无权 403"与"限流 403/429"（限流应当抛→502，不可当永久 deny）。"""
        if resp.status_code not in (403, 429):
            return False
        if resp.headers.get("x-ratelimit-remaining") == "0":
            return True
        if resp.headers.get("retry-after"):
            return True
        body = (resp.text or "").lower()
        return ("rate limit" in body) or ("secondary rate" in body) or ("abuse" in body)

    async def list_user_installations(self, *, user_token: str) -> list[int]:
        """该用户可见的 App 安装 id 列表（GET /user/installations，读 installations 键 + 分页）。"""
        out: list[int] = []
        page = 1
        while True:
            resp = await self._get_user(user_token, f"/user/installations?per_page=100&page={page}")
            resp.raise_for_status()
            items = resp.json().get("installations", [])
            if not items:
                break
            out.extend(i["id"] for i in items)
            if len(items) < 100:
                break
            page += 1
        return out

    async def resolve_repo_role(self, *, token: str, repo: str, principal):
        """解析用户在仓 repo(=full_name) 的 ScmRole。token=user-to-server，principal=GitHub username(=scm_login)。"""
        from src.service.scm.base import ScmRole
        from src.service.scm.scm_roles import github_role_to_scm
        if not principal:                       # B3：scm_login 缺失 → 不拼 None URL
            return ScmRole.NOT_VISIBLE
        resp = await self._get_user(token, f"/repos/{repo}/collaborators/{principal}/permission")
        sc = resp.status_code
        if sc == 200:
            data = resp.json()
            return github_role_to_scm(data.get("role_name"), data.get("permission"))
        if self._is_rate_limited(resp):
            resp.raise_for_status()             # 限流 → 抛（上层 502）
        if sc == 401:
            return ScmRole.NOT_VISIBLE          # token 吊销/失效 → fail-closed
        if sc in (403, 404):
            return await self._repo_read_fallback(token, repo)   # 只读用户 403 / 不存在 404 → 自查
        resp.raise_for_status()                 # 5xx 等 → 抛
        return ScmRole.NOT_VISIBLE              # 理论不可达

    async def _repo_read_fallback(self, user_token: str, repo: str):
        """I2：permission 端点对只读 collaborator 可能 403 → 用用户自己 token 自查 GET /repos/{repo}。
        200=能读（≥read）→ CAN_QUERY；否则 NOT_VISIBLE。"""
        from src.service.scm.base import ScmRole
        resp = await self._get_user(user_token, f"/repos/{repo}")
        if resp.status_code == 200:
            return ScmRole.CAN_QUERY
        return ScmRole.NOT_VISIBLE
```

- [ ] **Step 4: 跑测试确认通过**（8 passed）+ 确认既有 github_app 测试不回归：
```
cd /Users/java/ke-github-connect && KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth/test_github_repo_role.py tests/test_auth/test_github_login_identity.py -v
```

- [ ] **Step 5: 提交**
```bash
git add src/service/scm/github_app.py tests/test_auth/test_github_repo_role.py && git commit -m "feat(scm): GitHub resolve_repo_role（user token/状态矩阵/只读回退）+ list_user_installations"
```

---

## Task 3：GitLabOidcProvider 仓权限解析（Members API + sub 守卫）

**Files:** Modify `src/service/scm/gitlab_oidc.py`；Test `tests/test_auth/test_gitlab_repo_role.py`

- [ ] **Step 1: 失败测试**
```python
# tests/test_auth/test_gitlab_repo_role.py
import pytest
from src.service.scm.gitlab_oidc import GitLabOidcProvider
from src.service.scm.config import GitLabOidcConfig
from src.service.scm.base import ScmRole

ISS = "https://gitlab.example.com"


def _provider():
    return GitLabOidcProvider(GitLabOidcConfig(issuer=ISS, client_id="c", client_secret="s"))


@pytest.mark.asyncio
async def test_maintainer_can_bind(httpx_mock):
    httpx_mock.add_response(url=f"{ISS}/api/v4/projects/42/members/all/7", json={"access_level": 40})
    p = _provider()
    assert await p.resolve_repo_role(token="AT", repo=42, principal="7") == ScmRole.CAN_BIND


@pytest.mark.asyncio
async def test_reporter_can_query(httpx_mock):
    httpx_mock.add_response(url=f"{ISS}/api/v4/projects/42/members/all/7", json={"access_level": 20})
    p = _provider()
    assert await p.resolve_repo_role(token="AT", repo=42, principal="7") == ScmRole.CAN_QUERY


@pytest.mark.asyncio
async def test_guest_not_visible(httpx_mock):
    httpx_mock.add_response(url=f"{ISS}/api/v4/projects/42/members/all/7", json={"access_level": 10})
    p = _provider()
    assert await p.resolve_repo_role(token="AT", repo=42, principal="7") == ScmRole.NOT_VISIBLE


@pytest.mark.asyncio
async def test_non_member_404_not_visible(httpx_mock):
    httpx_mock.add_response(url=f"{ISS}/api/v4/projects/42/members/all/7", status_code=404)
    p = _provider()
    assert await p.resolve_repo_role(token="AT", repo=42, principal="7") == ScmRole.NOT_VISIBLE


@pytest.mark.asyncio
async def test_401_not_visible(httpx_mock):
    httpx_mock.add_response(url=f"{ISS}/api/v4/projects/42/members/all/7", status_code=401)
    p = _provider()
    assert await p.resolve_repo_role(token="AT", repo=42, principal="7") == ScmRole.NOT_VISIBLE


@pytest.mark.asyncio
async def test_non_numeric_sub_guarded(httpx_mock):
    # 非数字 sub → 不发请求、NOT_VISIBLE（守卫）
    p = _provider()
    assert await p.resolve_repo_role(token="AT", repo=42, principal="not-a-number") == ScmRole.NOT_VISIBLE
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_uses_bearer(httpx_mock):
    httpx_mock.add_response(url=f"{ISS}/api/v4/projects/42/members/all/7", json={"access_level": 30})
    p = _provider()
    await p.resolve_repo_role(token="AT_XYZ", repo=42, principal="7")
    assert httpx_mock.get_requests()[0].headers["Authorization"] == "Bearer AT_XYZ"
```

- [ ] **Step 2: 跑测试确认失败**（应在收集/导入期就红："为正确原因失败"——新模块未建→`ImportError`；已有类未加方法→`AttributeError: ... 'resolve_repo_role'`）。

- [ ] **Step 3: 实现**（追加到 `GitLabOidcProvider` 类内，`get_login_identity` 之后；文件已 import `httpx`、有 `self._cfg.issuer`）。**必做**：文件顶部加 `import logging` 与模块级 `_log = logging.getLogger("ke.scm.gitlab")`——`gitlab_oidc.py` 当前**两者都没有**，缺了 `_log.warning` 会 `NameError`（非数字 sub 守卫 + 测试会红）。
```python
    async def _get_authed(self, access_token: str, path: str) -> httpx.Response:
        """用用户 access_token 发 GET（GitLab REST）。返回原始 Response。"""
        async with httpx.AsyncClient(timeout=15) as client:
            return await client.get(f"{self._cfg.issuer}{path}",
                                    headers={"Authorization": f"Bearer {access_token}"})

    async def resolve_repo_role(self, *, token: str, repo, principal):
        """解析用户在 project(repo=project_id) 的 ScmRole。token=用户 access_token，principal=user_id(=gitlab_sub)。"""
        from src.service.scm.base import ScmRole
        from src.service.scm.scm_roles import gitlab_access_level_to_scm
        # B3：gitlab_sub 须为 numeric user id；非数字 → 告警 + NOT_VISIBLE（便于上线核实 sub=user id）
        if principal is None or not str(principal).isdigit():
            _log.warning("gitlab resolve_repo_role: 非数字 user_id(sub)=%r，无法核实仓权限", principal)
            return ScmRole.NOT_VISIBLE
        resp = await self._get_authed(token, f"/api/v4/projects/{repo}/members/all/{principal}")
        sc = resp.status_code
        if sc == 200:
            return gitlab_access_level_to_scm(int(resp.json()["access_level"]))
        if sc in (401, 404):
            return ScmRole.NOT_VISIBLE
        resp.raise_for_status()       # 5xx → 抛（上层 502）
        return ScmRole.NOT_VISIBLE
```

- [ ] **Step 4: 跑测试确认通过**（7 passed）+ 既有 gitlab 测试不回归（`tests/test_auth/test_gitlab_oidc.py`）。

- [ ] **Step 5: 提交**
```bash
git add src/service/scm/gitlab_oidc.py tests/test_auth/test_gitlab_repo_role.py && git commit -m "feat(scm): GitLab resolve_repo_role（Members API /all + sub 守卫 + 状态矩阵）"
```

---

## Task 4：build_refresh_fn（async 闭包 / discovery / expires_at / body-error invalid_grant）

**Files:** Create `src/service/scm/scm_refresh.py`；Test `tests/test_auth/test_scm_refresh.py`

- [ ] **Step 1: 失败测试**
```python
# tests/test_auth/test_scm_refresh.py
from datetime import datetime
import pytest
from src.service.scm.scm_refresh import build_refresh_fn
from src.service.scm.scm_token_store import ScmTokenInvalid
from src.service.scm.config import OAuthConfig, GitHubOAuthConfig, GitLabOidcConfig
from src.service.scm.gitlab_oidc import GitLabOidcProvider

GH_TOKEN_URL = "https://github.com/login/oauth/access_token"
ISS = "https://gitlab.example.com"


def _gh_cfg():
    return OAuthConfig(redirect_base="https://ke", github=GitHubOAuthConfig("cid", "sec"), gitlab=None)


@pytest.mark.asyncio
async def test_github_refresh_success_expires_at_is_datetime(httpx_mock):
    httpx_mock.add_response(url=GH_TOKEN_URL, json={"access_token": "NEW", "refresh_token": "R2", "expires_in": 3600})
    fn = build_refresh_fn("github", oauth_cfg=_gh_cfg())
    out = await fn("R1")
    assert out["access_token"] == "NEW" and out["refresh_token"] == "R2"
    assert isinstance(out["expires_at"], datetime)   # 非 int


@pytest.mark.asyncio
async def test_github_refresh_invalid_grant_raises(httpx_mock):
    # GitHub 返 HTTP 200 + body error
    httpx_mock.add_response(url=GH_TOKEN_URL, json={"error": "bad_refresh_token"})
    fn = build_refresh_fn("github", oauth_cfg=_gh_cfg())
    with pytest.raises(ScmTokenInvalid):
        await fn("R1")


@pytest.mark.asyncio
async def test_github_bad_creds_401_not_token_invalid(httpx_mock):
    # 运维配错 client creds（401）不可误判为 invalid_grant（否则会删用户关联）
    httpx_mock.add_response(url=GH_TOKEN_URL, status_code=401, json={"message": "Bad credentials"})
    fn = build_refresh_fn("github", oauth_cfg=_gh_cfg())
    with pytest.raises(Exception) as ei:
        await fn("R1")
    assert not isinstance(ei.value, ScmTokenInvalid)


def test_github_no_creds_returns_none():
    assert build_refresh_fn("github", oauth_cfg=OAuthConfig(redirect_base="x", github=None, gitlab=None)) is None


@pytest.mark.asyncio
async def test_gitlab_refresh_uses_discovered_endpoint(httpx_mock):
    httpx_mock.add_response(url=f"{ISS}/.well-known/openid-configuration",
                            json={"issuer": ISS, "authorization_endpoint": f"{ISS}/oauth/authorize",
                                  "token_endpoint": f"{ISS}/oauth/token", "jwks_uri": f"{ISS}/keys",
                                  "id_token_signing_alg_values_supported": ["RS256"]})
    httpx_mock.add_response(url=f"{ISS}/oauth/token", json={"access_token": "NEW", "expires_in": 7200})
    prov = GitLabOidcProvider(GitLabOidcConfig(issuer=ISS, client_id="c", client_secret="s"))
    fn = build_refresh_fn("gitlab", gitlab_provider=prov)
    out = await fn("R1")
    assert out["access_token"] == "NEW" and isinstance(out["expires_at"], datetime)


@pytest.mark.asyncio
async def test_gitlab_refresh_invalid_grant_raises(httpx_mock):
    httpx_mock.add_response(url=f"{ISS}/.well-known/openid-configuration",
                            json={"issuer": ISS, "authorization_endpoint": f"{ISS}/a",
                                  "token_endpoint": f"{ISS}/oauth/token", "jwks_uri": f"{ISS}/k",
                                  "id_token_signing_alg_values_supported": ["RS256"]})
    httpx_mock.add_response(url=f"{ISS}/oauth/token", status_code=400, json={"error": "invalid_grant"})
    prov = GitLabOidcProvider(GitLabOidcConfig(issuer=ISS, client_id="c", client_secret="s"))
    fn = build_refresh_fn("gitlab", gitlab_provider=prov)
    with pytest.raises(ScmTokenInvalid):
        await fn("R1")
```

- [ ] **Step 2: 跑测试确认失败**（应在收集/导入期就红："为正确原因失败"——新模块未建→`ImportError`；已有类未加方法→`AttributeError: ... 'resolve_repo_role'`）。

- [ ] **Step 3: 实现**
```python
# src/service/scm/scm_refresh.py
"""per-provider token 刷新闭包（get_valid_scm_token 的 refresh_fn）。设计 §4.5。
按 body error 判 invalid_grant（GitHub 200+error / GitLab 400+error）；expires_in→expires_at datetime。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from src.service.scm.scm_token_store import RefreshFn, ScmTokenInvalid

_GH_TOKEN_URL = "https://github.com/login/oauth/access_token"
_INVALID = {"invalid_grant", "bad_refresh_token"}


def _to_token_dict(body: dict) -> dict:
    """token 端点响应 → get_valid_scm_token 契约 dict（expires_in 秒 → expires_at aware datetime）。"""
    exp = None
    if body.get("expires_in"):
        exp = datetime.now(timezone.utc) + timedelta(seconds=int(body["expires_in"]))
    return {"access_token": body["access_token"], "refresh_token": body.get("refresh_token"), "expires_at": exp}


def build_refresh_fn(provider: str, *, gitlab_provider=None, oauth_cfg=None) -> Optional[RefreshFn]:
    """构造 provider 的 async refresh 闭包。无凭证/无 refresh 能力 → None。"""
    if provider == "github":
        gh = getattr(oauth_cfg, "github", None) if oauth_cfg else None
        if gh is None:
            return None        # 普通 OAuth-App token 无 refresh → 不刷新

        async def _gh_refresh(refresh_token: str) -> dict:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(_GH_TOKEN_URL, headers={"Accept": "application/json"},
                                      data={"client_id": gh.client_id, "client_secret": gh.client_secret,
                                            "grant_type": "refresh_token", "refresh_token": refresh_token})
            # GitHub 坏凭证等 → HTTP 4xx/5xx：raise_for_status 抛非 ScmTokenInvalid（保住关联行）
            r.raise_for_status()
            body = r.json()        # GitHub 成功/invalid_grant 都是 HTTP 200，区别在 body.error
            if body.get("error") in _INVALID:
                raise ScmTokenInvalid(f"github refresh: {body['error']}")
            if "access_token" not in body:
                raise RuntimeError(f"github refresh 无 access_token：{body.get('error')}")
            return _to_token_dict(body)
        return _gh_refresh

    if provider == "gitlab":
        if gitlab_provider is None:
            return None

        async def _gl_refresh(refresh_token: str) -> dict:
            disc = await gitlab_provider._discovery()      # 同包，token_endpoint 经 discovery（自管实例端点不固定）
            cfg = gitlab_provider._cfg
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(disc["token_endpoint"], headers={"Accept": "application/json"},
                                      data={"client_id": cfg.client_id, "client_secret": cfg.client_secret,
                                            "grant_type": "refresh_token", "refresh_token": refresh_token})
            body = {}
            try:
                body = r.json()
            except Exception:      # noqa: BLE001 — 非 JSON 响应
                body = {}
            if body.get("error") in _INVALID:     # GitLab invalid_grant 走 HTTP 400 + body.error
                raise ScmTokenInvalid(f"gitlab refresh: {body['error']}")
            r.raise_for_status()   # 其他非 2xx（坏凭证/5xx）→ 抛非 ScmTokenInvalid
            if "access_token" not in body:
                raise RuntimeError("gitlab refresh 无 access_token")
            return _to_token_dict(body)
        return _gl_refresh

    return None
```

- [ ] **Step 4: 跑测试确认通过**（6 passed）。

- [ ] **Step 5: 提交**
```bash
git add src/service/scm/scm_refresh.py tests/test_auth/test_scm_refresh.py && git commit -m "feat(scm): build_refresh_fn（async/discovery/expires_at/body-error invalid_grant）"
```

---

## Task 5：权限缓存（仅正向）+ resolve_repo_role_cached + 全回归

**Files:** Create `src/service/scm/scm_perm_cache.py`；Test `tests/test_auth/test_scm_perm_cache.py`

> 缓存 key `(user_id, connection_id, repo_external_id)`；`connection_id`（ScmConnection.id 全局唯一）已隐含 provider。**只缓存非 NOT_VISIBLE（正向）**；deny 不缓存。`can_bind` 路径不经此（裸调）。

- [ ] **Step 1: 失败测试**
```python
# tests/test_auth/test_scm_perm_cache.py
import pytest
from src.service.scm.base import ScmRole
from src.service.scm.scm_perm_cache import resolve_repo_role_cached, cache_clear, cache_invalidate


@pytest.fixture(autouse=True)
def _clear():
    cache_clear()
    yield
    cache_clear()


class _FakeProvider:
    def __init__(self, role, *, fail_if_called_twice=False):
        self._role = role
        self.calls = 0
    async def resolve_repo_role(self, *, token, repo, principal):
        self.calls += 1
        return self._role


@pytest.mark.asyncio
async def test_positive_cached_second_call_no_api():
    p = _FakeProvider(ScmRole.CAN_QUERY)
    kw = dict(user_id=1, connection_id="c1", repo_external_id=42, token="t", repo="o/r", principal="u")
    assert await resolve_repo_role_cached(p, **kw) == ScmRole.CAN_QUERY
    assert await resolve_repo_role_cached(p, **kw) == ScmRole.CAN_QUERY
    assert p.calls == 1   # 第二次命中缓存，不再调 provider


@pytest.mark.asyncio
async def test_deny_not_cached():
    p = _FakeProvider(ScmRole.NOT_VISIBLE)
    kw = dict(user_id=1, connection_id="c1", repo_external_id=42, token="t", repo="o/r", principal="u")
    assert await resolve_repo_role_cached(p, **kw) == ScmRole.NOT_VISIBLE
    assert await resolve_repo_role_cached(p, **kw) == ScmRole.NOT_VISIBLE
    assert p.calls == 2   # NOT_VISIBLE 不缓存，第二次重打


@pytest.mark.asyncio
async def test_can_bind_not_cached():
    # A3：can_bind 永不缓存——即使经 cached wrapper，CAN_BIND 结果也不入缓存
    p = _FakeProvider(ScmRole.CAN_BIND)
    kw = dict(user_id=1, connection_id="c1", repo_external_id=42, token="t", repo="o/r", principal="u")
    assert await resolve_repo_role_cached(p, **kw) == ScmRole.CAN_BIND
    assert await resolve_repo_role_cached(p, **kw) == ScmRole.CAN_BIND
    assert p.calls == 2   # CAN_BIND 不缓存，第二次重打


@pytest.mark.asyncio
async def test_invalidate():
    p = _FakeProvider(ScmRole.CAN_QUERY)
    kw = dict(user_id=1, connection_id="c1", repo_external_id=42, token="t", repo="o/r", principal="u")
    await resolve_repo_role_cached(p, **kw)
    cache_invalidate("c1")
    await resolve_repo_role_cached(p, **kw)
    assert p.calls == 2   # invalidate 后重打


@pytest.mark.asyncio
async def test_ttl_expiry(monkeypatch):
    import src.service.scm.scm_perm_cache as mod
    p = _FakeProvider(ScmRole.CAN_QUERY)
    kw = dict(user_id=1, connection_id="c1", repo_external_id=42, token="t", repo="o/r", principal="u")
    t = {"now": 1000.0}
    monkeypatch.setattr(mod, "_now", lambda: t["now"])
    await resolve_repo_role_cached(p, **kw)
    t["now"] += mod._TTL + 1     # 过期
    await resolve_repo_role_cached(p, **kw)
    assert p.calls == 2
```

- [ ] **Step 2: 跑测试确认失败**（应在收集/导入期就红："为正确原因失败"——新模块未建→`ImportError`；已有类未加方法→`AttributeError: ... 'resolve_repo_role'`）。

- [ ] **Step 3: 实现**
```python
# src/service/scm/scm_perm_cache.py
"""SCM 仓权限缓存：仅缓存正向 grant 的 can_query 结果（TTL）。设计 §4.6。
can_bind 不经此（裸调 resolve_repo_role）；deny 不缓存（吊销后不长期放行）。per-process。"""
from __future__ import annotations

import time
from typing import Optional

from src.service.scm.base import ScmRole

_TTL = 300  # 秒
# key=(user_id, connection_id, repo_external_id) -> (ScmRole, expires_at_epoch)
_CACHE: dict[tuple, tuple] = {}


def _now() -> float:
    return time.time()


def cache_clear() -> None:
    """清空全部缓存（测试 autouse）。"""
    _CACHE.clear()


def cache_invalidate(connection_id: str) -> None:
    """清除某连接的所有缓存项（token 轮换/连接删除/re-link 时由上层调）。"""
    for k in [k for k in _CACHE if k[1] == connection_id]:
        _CACHE.pop(k, None)


def _gc(now: float) -> None:
    """机会式清过期键，限制无界增长。"""
    for k in [k for k, (_, exp) in _CACHE.items() if exp < now]:
        _CACHE.pop(k, None)


async def resolve_repo_role_cached(provider_obj, *, user_id: int, connection_id: str,
                                   repo_external_id, token: str, repo, principal) -> ScmRole:
    """带缓存的仓权限解析（can_query 路径用）。仅缓存正向 CAN_QUERY（can_bind 永不缓存、deny 不缓存）。"""
    now = _now()
    _gc(now)
    key = (user_id, connection_id, repo_external_id)
    hit = _CACHE.get(key)
    if hit is not None and hit[1] >= now:
        return hit[0]
    role = await provider_obj.resolve_repo_role(token=token, repo=repo, principal=principal)
    if role == ScmRole.CAN_QUERY:        # 仅缓存正向 CAN_QUERY；can_bind 永不缓存(A3)、deny 不缓存
        _CACHE[key] = (role, now + _TTL)
    return role
```

- [ ] **Step 4: 跑测试确认通过**（5 passed：含 can_bind 不缓存）。

- [ ] **Step 5: P4b-0 全回归 + import 冒烟**
```
cd /Users/java/ke-github-connect && KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth/test_scm_roles.py tests/test_auth/test_github_repo_role.py tests/test_auth/test_gitlab_repo_role.py tests/test_auth/test_scm_refresh.py tests/test_auth/test_scm_perm_cache.py -v
cd /Users/java/ke-github-connect && KE_JWT_SECRET=test KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -c "import src.service.api; print('api OK')"
```
Expected: 全 PASS；api OK（P4b-0 纯库，不应影响装配）。

- [ ] **Step 6: 提交**
```bash
git add src/service/scm/scm_perm_cache.py tests/test_auth/test_scm_perm_cache.py && git commit -m "feat(scm): 仓权限正向缓存 + resolve_repo_role_cached + invalidate/clear"
```

---

## 完成标准（P4b-0 Done，对齐 spec §8）
1. 角色映射全档位（含 Guest-trap、自定义 role_name 回退 permission、大小写）。
2. GitHub `resolve_repo_role`：user token（断言 header + get_installation_token 未调用）；401/非限流403/404→NOT_VISIBLE、限流/5xx→抛；只读 403→回退 CAN_QUERY；scm_login=None→NOT_VISIBLE。`list_user_installations` 读 installations 键 + 分页。
3. GitLab `resolve_repo_role`：各 access_level；401/404→NOT_VISIBLE；非数字 sub→守卫 NOT_VISIBLE；Bearer header。
4. `build_refresh_fn`：expires_at 为 datetime；GitLab 命中 discovered 端点；invalid_grant(body)→ScmTokenInvalid；坏 creds 401→非 ScmTokenInvalid；GitHub 无 creds→None。
5. 缓存：正向命中、deny 不缓存、TTL 过期重取、invalidate/clear。
6. P4b-0 全回归绿 + import api 冒烟过；无路由改动。

> **上线前核实（M3/M4，P4b-1 staging gated/手动，不阻塞 P4b-0 单测）**：单测全用 mock，以下真实 API 假设需在接入前核实：(a) GitHub `/collaborators/{u}/permission` 200 响应确有顶层 `role_name`+`permission`；(b) **只读 collaborator 查自身确返 403（而非 200+低权）**——这是只读回退分支的最弱假设；(c) GitLab `members/all/{user_id}` 对 numeric user_id 返 `access_level`，且 `gitlab_sub`(OIDC sub) 即该 numeric user id。

## 待后续（P4b-1 / P4b-2 衔接）
- P4b-1：bind 门（裸 resolve_repo_role，读 body.* + scm_login 作 principal）+ QA `/explain` 门（resolve_repo_role_cached，未绑定走 KE-RBAC）+ authorize() 交集 + kill-switch + 在 token 轮换/连接删除处调 cache_invalidate；均经 get_valid_scm_token + build_refresh_fn 取 user token，ScmTokenInvalid→403 重关联。
- P4b-2：install-url mint_state+csrf(Lax) / callback consume_state + list_user_installations 核归属 + 先核验后写；解决 install-先于-link 矛盾。
