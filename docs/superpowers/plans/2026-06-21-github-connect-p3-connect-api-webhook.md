# GitHub 连接 P3：连接 onboarding API + webhook + 接通真实 worker

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans，逐任务执行。Steps 用 `- [ ]`。

**Goal:** 把 P1（SCM provider）+ P2（作业系统/worker）接成**端到端可演示**：用户连接 GitHub App → 选仓/选分支 → 绑定到工程 → 自动入队 → ke-indexer 真实 clone+索引；并支持 push webhook 自动增量同步。

**Architecture:** 新增 `scm_router`（连接生命周期 + 列仓/列分支）+ `webhook_router`（GitHub webhook，HMAC 验签→去重→入队）+ 扩展 project 绑定与索引触发端点。`Project` 加绑定列（scm_connection_id / repo_external_id / repo_full_name / ref / ref_type / subpath；保留旧 git_* 列向后兼容）。`indexer.py::_main` 接通真实 worker 装配（job→project→scm_connection→GitHubAppProvider→make_real_indexer）。

**Tech Stack:** FastAPI / SQLAlchemy async / Alembic / httpx / hmac(stdlib) / pytest + FastAPI TestClient。复用 P1 `GitHubAppProvider`、P2 `enqueue_index_job`/`run_worker_loop`/`make_real_indexer`、现有 auth(`get_current_user`/`require_project_role`)。

**设计依据:** GitHub仓库连接-设计.md §8/§9/§12/§5.2、身份与授权模型-设计.md（注：**SCM-role 绑定门禁 + 按需权限校验属 P4**；P3 仅用现有 KE RBAC 管"谁能建/绑工程"，并预留挂点）。

**前置:** P1+P2 已在本分支。worktree `/Users/java/ke-github-connect`；测试 `./venv/bin/python -m pytest`（import 报 env 时按 P1 方式加 KE_JWT_SECRET/KE_TOKEN_ENC_KEY）。注入式：所有路由用工厂 `create_*_routes(deps)` 或依赖覆盖，测试用 `TestClient` + override `get_current_user`/`get_db` + 注入 fake provider（**不打真实 GitHub**）。

**范围边界:** P3 不做：OAuth/OIDC 登录与身份映射、SCM-role 绑定门禁、按需权限校验（全部 P4）；不做加密落盘/健康度面板（P5）；不做前端（P6）。callback 暂用 installation_id 建连接，**严格归属核验留 P4**。

---

## 文件结构

| 文件 | 责任 |
|---|---|
| `src/service/db_models_homepage.py`（改） | `Project` 加绑定列 |
| `alembic/versions/<id>_project_scm_binding.py` | projects 加绑定列 |
| `src/service/scm/provider_factory.py` | `get_github_provider()` 单例（按 env 配置造 GitHubAppProvider；测试可 override） |
| `src/service/scm_router.py` | install-url / callback / connections(list,delete) / repos / branches |
| `src/service/scm_binding_router.py` | POST /projects/{id}/bind + /reindex + GET /index-status |
| `src/service/webhook_router.py` | POST /webhooks/github（HMAC→parse→dedup→enqueue） |
| `src/service/scm/webhook_verify.py` | `verify_signature` + `parse_push` 纯函数 |
| `src/service/indexer.py`（改） | `_main` 接通真实 worker 装配 |
| `src/service/api.py`（改） | 挂三个 router |
| `tests/test_auth/test_scm_router.py` / `test_scm_binding.py` / `test_webhook_router.py` / `test_webhook_verify.py` / `test_project_binding_model.py` / `test_indexer_assembly.py` | 测试 |

---

## Task 1: `Project` 绑定列 + 迁移

**Files:** Modify `src/service/db_models_homepage.py`（`Project` 加列）；Create `alembic/versions/project_scm_binding_v1.py`；Test `tests/test_auth/test_project_binding_model.py`

- [ ] **Step 1: 失败测试**
```python
# tests/test_auth/test_project_binding_model.py
import pytest, pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select
from src.service.db_models_homepage import Base, Project


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s


@pytest.mark.asyncio
async def test_project_scm_binding_fields(session):
    p = Project(id="p1", name="P1", scm_connection_id="conn-1", repo_external_id=42,
                repo_full_name="o/r", ref="master", ref_type="branch", subpath="mall-portal")
    session.add(p); await session.commit()
    row = (await session.execute(select(Project).where(Project.id == "p1"))).scalar_one()
    assert row.scm_connection_id == "conn-1"
    assert row.repo_external_id == 42
    assert row.repo_full_name == "o/r"
    assert row.ref == "master"
    assert row.ref_type == "branch"
    assert row.subpath == "mall-portal"


@pytest.mark.asyncio
async def test_binding_fields_default_none(session):
    p = Project(id="p2", name="P2")
    session.add(p); await session.commit()
    row = (await session.execute(select(Project).where(Project.id == "p2"))).scalar_one()
    assert row.scm_connection_id is None
    assert row.ref_type is None
```

- [ ] **Step 2: 跑测试确认失败**（TypeError: unexpected keyword 'scm_connection_id'）。

- [ ] **Step 3: 给 `Project` 加列**（在 `Project` 类内、现有 git_* 列附近；保留 git_branch 等旧列不动）：
```python
    # ── SCM 绑定（P3，设计 §5.2）；保留上方 git_* 旧列向后兼容 ──
    scm_connection_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("scm_connections.id", ondelete="SET NULL"), nullable=True
    )
    repo_external_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)  # 仓库 numeric id（绑定主键）
    repo_full_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)   # owner/repo（展示）
    ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)              # 分支/tag/sha
    ref_type: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)          # branch|tag|sha
    subpath: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)          # monorepo 子目录
```

- [ ] **Step 4: 跑测试确认通过**（2 passed）。

- [ ] **Step 5: 迁移**（先 `./venv/bin/alembic heads`，head 应为 `index_jobs_v1`）：
```python
# alembic/versions/project_scm_binding_v1.py
"""project scm binding v1: projects 加 SCM 绑定列

Revision ID: project_scm_binding_v1
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "project_scm_binding_v1"
down_revision: Union[str, Sequence[str], None] = "index_jobs_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("scm_connection_id", sa.String(length=64), nullable=True))
    op.add_column("projects", sa.Column("repo_external_id", sa.BigInteger(), nullable=True))
    op.add_column("projects", sa.Column("repo_full_name", sa.String(length=255), nullable=True))
    op.add_column("projects", sa.Column("ref", sa.String(length=255), nullable=True))
    op.add_column("projects", sa.Column("ref_type", sa.String(length=16), nullable=True))
    op.add_column("projects", sa.Column("subpath", sa.String(length=512), nullable=True))
    op.create_foreign_key(
        "fk_projects_scm_connection", "projects", "scm_connections",
        ["scm_connection_id"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_projects_scm_connection", "projects", type_="foreignkey")
    for col in ("subpath", "ref_type", "ref", "repo_full_name", "repo_external_id", "scm_connection_id"):
        op.drop_column("projects", col)
```
确认单一 head = `project_scm_binding_v1`。

- [ ] **Step 6: 提交**
```bash
git add src/service/db_models_homepage.py alembic/versions/project_scm_binding_v1.py tests/test_auth/test_project_binding_model.py
git commit -m "feat(scm): projects 加 SCM 绑定列 + 迁移"
```

---

## Task 2: provider 工厂 + `GET /scm/github/install-url`

**Files:** Create `src/service/scm/provider_factory.py` + `src/service/scm_router.py`；Modify `src/service/api.py`；Test `tests/test_auth/test_scm_router.py`

> install-url 形如 `https://github.com/apps/<slug>/installations/new?state=<state>`；slug 从 env `KE_GH_APP_SLUG`。state 用 `secrets.token_urlsafe` 防 CSRF（v1 简化：返回随机 state，前端回带；严格 state 存储校验可后续加）。

- [ ] **Step 1: 失败测试**
```python
# tests/test_auth/test_scm_router.py
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.service.scm_router import create_scm_routes


class _User:
    username = "alice"; is_admin = True


def _app(provider=None):
    app = FastAPI()
    app.include_router(create_scm_routes(
        get_current_user=lambda: _User(),
        get_db=None,            # 本测试不触 DB 的端点用不到
        get_provider=lambda: provider,
        app_slug="ke-test-app",
    ))
    return app


def test_install_url():
    c = TestClient(_app())
    r = c.get("/scm/github/install-url")
    assert r.status_code == 200
    body = r.json()
    assert "github.com/apps/ke-test-app/installations/new" in body["install_url"]
    assert body["state"]            # 非空 state
```

- [ ] **Step 2: 跑测试确认失败**（ImportError create_scm_routes）。

- [ ] **Step 3: 实现 provider 工厂**
```python
# src/service/scm/provider_factory.py
"""按 env 造 GitHubAppProvider 单例。测试用依赖覆盖注入 fake。"""
from __future__ import annotations

from functools import lru_cache

from src.service.scm.config import load_github_app_config
from src.service.scm.github_app import GitHubAppProvider


@lru_cache(maxsize=1)
def get_github_provider() -> GitHubAppProvider:
    return GitHubAppProvider(load_github_app_config())
```

- [ ] **Step 4: 实现 scm_router（本任务只放 install-url；后续任务往同文件加）**
```python
# src/service/scm_router.py
"""SCM 连接 onboarding 路由（注入式工厂，便于测试）。设计 §8/§12。"""
from __future__ import annotations

import os
import secrets
from typing import Callable, Optional

from fastapi import APIRouter, Depends


def create_scm_routes(*, get_current_user: Callable, get_db: Optional[Callable],
                      get_provider: Callable, app_slug: Optional[str] = None) -> APIRouter:
    router = APIRouter(prefix="/scm", tags=["scm"])
    slug = app_slug or os.getenv("KE_GH_APP_SLUG", "")

    @router.get("/github/install-url")
    async def install_url(user=Depends(get_current_user)) -> dict:
        """返回 GitHub App 安装 URL + 防 CSRF state（前端跳转后回带）。"""
        state = secrets.token_urlsafe(24)
        return {
            "install_url": f"https://github.com/apps/{slug}/installations/new?state={state}",
            "state": state,
        }

    return router
```

- [ ] **Step 5: 挂到 api.py**（与现有 include_router 一起；生产装配传真实依赖）
```python
from src.service.scm_router import create_scm_routes
from src.service.scm.provider_factory import get_github_provider
# ... app 装配处：
app.include_router(create_scm_routes(
    get_current_user=get_current_user, get_db=get_db, get_provider=get_github_provider,
))
```
（`get_current_user`/`get_db` 已在 api.py 导入；若无则从 `auth_dependencies`/`db` 导入。）

- [ ] **Step 6: 跑测试确认通过 + 冒烟 import api**
`./venv/bin/python -m pytest tests/test_auth/test_scm_router.py -v`（1 passed）；`./venv/bin/python -c "import src.service.api"`（确认挂载不报错；可能需 KE_JWT_SECRET/KE_TOKEN_ENC_KEY env）。

- [ ] **Step 7: 提交**
```bash
git add src/service/scm/provider_factory.py src/service/scm_router.py src/service/api.py tests/test_auth/test_scm_router.py
git commit -m "feat(scm): provider 工厂 + GET /scm/github/install-url + 挂载"
```

---

## Task 3: `GET /scm/github/callback` → 建 scm_connection

**Files:** Modify `src/service/scm_router.py`；Test `tests/test_auth/test_scm_router.py`(追加)

> callback 收 `installation_id` + `state`（GitHub App 安装后重定向带）。P3：用 installation_id 拉 account_login（经 provider）建 `ScmConnection(provider=github, auth_type=github_app, status=active)`。**严格归属核验（用户 OAuth token 确认 installation 属本人）留 P4**——本任务注释标注。

- [ ] **Step 1: 追加失败测试**（用 in-memory DB override get_db + fake provider）
```python
# 追加到 tests/test_auth/test_scm_router.py
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select
from src.service.db_models_homepage import Base, ScmConnection


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


class _FakeProvider:
    async def get_account_login(self, installation_id):
        return "macrozheng"


def _app_db(maker, provider):
    from fastapi import FastAPI
    app = FastAPI()
    async def _get_db():
        async with maker() as s:
            yield s
    app.include_router(create_scm_routes(
        get_current_user=lambda: _User(), get_db=_get_db,
        get_provider=lambda: provider, app_slug="ke-test-app",
    ))
    return app


@pytest.mark.asyncio
async def test_callback_creates_connection(maker):
    from fastapi.testclient import TestClient
    c = TestClient(_app_db(maker, _FakeProvider()))
    r = c.get("/scm/github/callback", params={"installation_id": 12345, "state": "s1"})
    assert r.status_code == 200
    cid = r.json()["connection_id"]
    async with maker() as s:
        conn = (await s.execute(select(ScmConnection).where(ScmConnection.id == cid))).scalar_one()
        assert conn.github_installation_id == 12345
        assert conn.provider == "github"
        assert conn.auth_type == "github_app"
        assert conn.account_login == "macrozheng"
        assert conn.status == "active"
```

- [ ] **Step 2: 跑测试确认失败**。

- [ ] **Step 3: 给 GitHubAppProvider 加 `get_account_login`**（`src/service/scm/github_app.py`，复用 `_get`）：
```python
    async def get_account_login(self, installation_id: int) -> str:
        """取该安装的账号 login（org/user 名）。GET /app/installations/{id} 需 App JWT。"""
        headers = {
            "Authorization": f"Bearer {self._app_jwt()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{_API}/app/installations/{installation_id}", headers=headers)
            resp.raise_for_status()
        return resp.json().get("account", {}).get("login", "")
```

- [ ] **Step 4: 给 scm_router 加 callback**（在 `create_scm_routes` 内、install_url 之后；需要 `get_db`、`uuid`、`from sqlalchemy.ext.asyncio import AsyncSession`、`from src.service.db_models_homepage import ScmConnection`，并在文件顶部 import `uuid`）：
```python
    @router.get("/github/callback")
    async def callback(installation_id: int, state: str = "", user=Depends(get_current_user),
                       db=Depends(get_db)) -> dict:
        """GitHub App 安装回调：建 scm_connection。
        TODO(P4)：用用户 OAuth user-to-server token 核实该 installation 确属当前用户（防伪造）。"""
        provider = get_provider()
        login = await provider.get_account_login(installation_id)
        conn = ScmConnection(
            id=f"conn-{uuid.uuid4().hex[:16]}", provider="github", auth_type="github_app",
            github_installation_id=installation_id, account_login=login, status="active",
            created_by=getattr(user, "username", None),
        )
        db.add(conn)
        await db.commit()
        return {"connection_id": conn.id, "account_login": login}
```

- [ ] **Step 5: 跑测试确认通过**（install-url + callback 都过）。

- [ ] **Step 6: 提交**
```bash
git add src/service/scm_router.py src/service/scm/github_app.py tests/test_auth/test_scm_router.py
git commit -m "feat(scm): GET /github/callback 建连接 + provider.get_account_login"
```

---

## Task 4: 连接列表/删除 `GET/DELETE /scm/connections`

**Files:** Modify `src/service/scm_router.py`；Test `tests/test_auth/test_scm_router.py`(追加)

- [ ] **Step 1: 追加失败测试**
```python
@pytest.mark.asyncio
async def test_list_and_delete_connections(maker):
    from fastapi.testclient import TestClient
    from src.service.db_models_homepage import ScmConnection
    async with maker() as s:
        s.add(ScmConnection(id="c1", provider="github", auth_type="github_app",
                            github_installation_id=1, account_login="o", status="active", created_by="alice"))
        await s.commit()
    c = TestClient(_app_db(maker, _FakeProvider()))
    r = c.get("/scm/connections")
    assert r.status_code == 200
    assert any(x["id"] == "c1" for x in r.json()["connections"])
    assert c.delete("/scm/connections/c1").status_code == 204
    assert all(x["id"] != "c1" for x in c.get("/scm/connections").json()["connections"])
```

- [ ] **Step 2: 跑测试确认失败**。

- [ ] **Step 3: 加路由**（`create_scm_routes` 内；`from sqlalchemy import select`、`from fastapi import HTTPException`、`Response`）：
```python
    @router.get("/connections")
    async def list_connections(user=Depends(get_current_user), db=Depends(get_db)) -> dict:
        rows = (await db.execute(
            select(ScmConnection).where(ScmConnection.created_by == getattr(user, "username", None))
        )).scalars().all()
        return {"connections": [
            {"id": r.id, "provider": r.provider, "auth_type": r.auth_type,
             "account_login": r.account_login, "status": r.status} for r in rows
        ]}

    @router.delete("/connections/{connection_id}", status_code=204)
    async def delete_connection(connection_id: str, user=Depends(get_current_user), db=Depends(get_db)):
        conn = await db.get(ScmConnection, connection_id)
        if conn is None:
            raise HTTPException(status_code=404, detail="连接不存在")
        if conn.created_by != getattr(user, "username", None) and not getattr(user, "is_admin", False):
            raise HTTPException(status_code=403, detail="无权删除该连接")
        await db.delete(conn)
        await db.commit()
        return Response(status_code=204)
```
（顶部补 import：`from fastapi import APIRouter, Depends, HTTPException, Response`、`from sqlalchemy import select`。）

- [ ] **Step 4: 跑测试确认通过**。

- [ ] **Step 5: 提交**
```bash
git add src/service/scm_router.py tests/test_auth/test_scm_router.py
git commit -m "feat(scm): GET/DELETE /scm/connections（user-scoped）"
```

---

## Task 5: 列仓 + 列分支 `GET /scm/connections/{id}/repos|branches`

**Files:** Modify `src/service/scm_router.py`；Test `tests/test_auth/test_scm_router.py`(追加)

> 用 P1 `provider.list_repos`/`list_branches`。**P3 不做成员关系过滤（"visible"过滤是 P4 权限）**——本任务返回 installation 可见仓，路径用 `/repos`；P4 再加 `/visible-repos` 过滤。

- [ ] **Step 1: 追加失败测试**（fake provider 加 list_repos/list_branches）
```python
class _FakeProvider2(_FakeProvider):
    async def list_repos(self, installation_id):
        from src.service.scm.base import RepoInfo
        return [RepoInfo(external_id=42, full_name="o/r", default_branch="master", private=True)]
    async def list_branches(self, installation_id, full_name):
        from src.service.scm.base import BranchList
        return BranchList(default_branch="master", branches=["master", "dev"])


@pytest.mark.asyncio
async def test_list_repos_and_branches(maker):
    from fastapi.testclient import TestClient
    from src.service.db_models_homepage import ScmConnection
    async with maker() as s:
        s.add(ScmConnection(id="c1", provider="github", auth_type="github_app",
                            github_installation_id=7, account_login="o", status="active", created_by="alice"))
        await s.commit()
    c = TestClient(_app_db(maker, _FakeProvider2()))
    repos = c.get("/scm/connections/c1/repos").json()["repos"]
    assert repos[0]["external_id"] == 42 and repos[0]["full_name"] == "o/r"
    br = c.get("/scm/connections/c1/repos/o%2Fr/branches").json()
    assert br["default_branch"] == "master" and "dev" in br["branches"]
```

- [ ] **Step 2: 跑测试确认失败**。

- [ ] **Step 3: 加路由**（`create_scm_routes` 内；用 `_load_conn` helper 取连接 + installation_id）
```python
    async def _load_conn(connection_id: str, user, db) -> "ScmConnection":
        conn = await db.get(ScmConnection, connection_id)
        if conn is None:
            raise HTTPException(status_code=404, detail="连接不存在")
        if conn.created_by != getattr(user, "username", None) and not getattr(user, "is_admin", False):
            raise HTTPException(status_code=403, detail="无权访问该连接")
        return conn

    @router.get("/connections/{connection_id}/repos")
    async def list_repos(connection_id: str, user=Depends(get_current_user), db=Depends(get_db)) -> dict:
        conn = await _load_conn(connection_id, user, db)
        repos = await get_provider().list_repos(conn.github_installation_id)
        return {"repos": [
            {"external_id": r.external_id, "full_name": r.full_name,
             "default_branch": r.default_branch, "private": r.private} for r in repos
        ]}

    @router.get("/connections/{connection_id}/repos/{full_name:path}/branches")
    async def list_branches(connection_id: str, full_name: str,
                            user=Depends(get_current_user), db=Depends(get_db)) -> dict:
        conn = await _load_conn(connection_id, user, db)
        bl = await get_provider().list_branches(conn.github_installation_id, full_name)
        return {"default_branch": bl.default_branch, "branches": bl.branches}
```
（`{full_name:path}` 允许 owner/repo 带斜杠。）

- [ ] **Step 4: 跑测试确认通过**。

- [ ] **Step 5: 提交**
```bash
git add src/service/scm_router.py tests/test_auth/test_scm_router.py
git commit -m "feat(scm): 列仓 + 列分支端点（复用 P1 provider）"
```

---

## Task 6: 绑定 + 触发索引 `POST /projects/{id}/bind|reindex` + `GET /index-status`

**Files:** Create `src/service/scm_binding_router.py`；Modify `src/service/api.py`；Test `tests/test_auth/test_scm_binding.py`

> bind：把连接/仓/分支/子目录写进 Project 绑定列 + 入队 `full_index`。**"谁能绑"用现有 KE RBAC**（admin 或 project owner/maintainer，`require_project_role`）；**SCM-role 门禁留 P4**（本任务注释标注挂点）。

- [ ] **Step 1: 失败测试**
```python
# tests/test_auth/test_scm_binding.py
import pytest, pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select
from src.service.db_models_homepage import Base, Project, IndexJob
from src.service.scm_binding_router import create_scm_binding_routes


class _User:
    username = "alice"; is_admin = True


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


def _app(maker):
    app = FastAPI()
    async def _get_db():
        async with maker() as s:
            yield s
    app.include_router(create_scm_binding_routes(get_current_user=lambda: _User(), get_db=_get_db))
    return app


@pytest.mark.asyncio
async def test_bind_writes_binding_and_enqueues(maker):
    async with maker() as s:
        s.add(Project(id="p1", name="P1")); await s.commit()
    c = TestClient(_app(maker))
    r = c.post("/projects/p1/bind", json={
        "connection_id": "c1", "repo_external_id": 42, "repo_full_name": "o/r",
        "ref": "master", "ref_type": "branch", "subpath": None,
    })
    assert r.status_code == 200
    assert r.json()["job_id"]
    async with maker() as s:
        p = (await s.execute(select(Project).where(Project.id == "p1"))).scalar_one()
        assert p.scm_connection_id == "c1" and p.repo_external_id == 42 and p.ref == "master"
        jobs = (await s.execute(select(IndexJob).where(IndexJob.project_id == "p1"))).scalars().all()
        assert len(jobs) == 1 and jobs[0].type == "full_index"


@pytest.mark.asyncio
async def test_reindex_enqueues(maker):
    async with maker() as s:
        s.add(Project(id="p1", name="P1", scm_connection_id="c1", repo_external_id=42,
                      repo_full_name="o/r", ref="master", ref_type="branch")); await s.commit()
    c = TestClient(_app(maker))
    assert c.post("/projects/p1/reindex").status_code == 200
    async with maker() as s:
        jobs = (await s.execute(select(IndexJob).where(IndexJob.project_id == "p1"))).scalars().all()
        assert len(jobs) == 1 and jobs[0].type == "reindex"


@pytest.mark.asyncio
async def test_index_status(maker):
    async with maker() as s:
        s.add(Project(id="p1", name="P1")); await s.commit()
    c = TestClient(_app(maker))
    c.post("/projects/p1/bind", json={"connection_id": "c1", "repo_external_id": 1,
           "repo_full_name": "o/r", "ref": "master", "ref_type": "branch", "subpath": None})
    st = c.get("/projects/p1/index-status").json()
    assert st["status"] in ("queued", "cloning")
```

- [ ] **Step 2: 跑测试确认失败**。

- [ ] **Step 3: 实现**
```python
# src/service/scm_binding_router.py
"""工程 SCM 绑定 + 索引触发。设计 §8/§9。
'谁能绑'用现有 KE RBAC；TODO(P4)：叠加 SCM-role 门禁（owner/maintainer 才能绑）。"""
from __future__ import annotations

from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, desc

from src.service.db_models_homepage import Project, IndexJob
from src.service.indexing.queue import enqueue_index_job


class BindRequest(BaseModel):
    connection_id: str
    repo_external_id: int
    repo_full_name: str
    ref: str
    ref_type: str = "branch"
    subpath: Optional[str] = None


def create_scm_binding_routes(*, get_current_user: Callable, get_db: Callable) -> APIRouter:
    router = APIRouter(tags=["scm-binding"])

    @router.post("/projects/{project_id}/bind")
    async def bind(project_id: str, body: BindRequest,
                   user=Depends(get_current_user), db=Depends(get_db)) -> dict:
        p = await db.get(Project, project_id)
        if p is None:
            raise HTTPException(status_code=404, detail="工程不存在")
        # TODO(P4)：SCM-role 门禁——校验 user 在该仓是 owner/maintainer 才放行。
        p.scm_connection_id = body.connection_id
        p.repo_external_id = body.repo_external_id
        p.repo_full_name = body.repo_full_name
        p.ref = body.ref
        p.ref_type = body.ref_type
        p.subpath = body.subpath
        p.status = "indexing"
        job = await enqueue_index_job(db, project_id=project_id, type_="full_index", trigger="manual")
        await db.commit()
        return {"job_id": job.id}

    @router.post("/projects/{project_id}/reindex")
    async def reindex(project_id: str, user=Depends(get_current_user), db=Depends(get_db)) -> dict:
        p = await db.get(Project, project_id)
        if p is None:
            raise HTTPException(status_code=404, detail="工程不存在")
        job = await enqueue_index_job(db, project_id=project_id, type_="reindex", trigger="manual")
        await db.commit()
        return {"job_id": job.id}

    @router.get("/projects/{project_id}/index-status")
    async def index_status(project_id: str, user=Depends(get_current_user), db=Depends(get_db)) -> dict:
        job = (await db.execute(
            select(IndexJob).where(IndexJob.project_id == project_id)
            .order_by(desc(IndexJob.created_at)).limit(1)
        )).scalars().first()
        if job is None:
            raise HTTPException(status_code=404, detail="无索引作业")
        return {"job_id": job.id, "status": job.status, "progress": job.progress, "error": job.error}

    return router
```

- [ ] **Step 4: 挂 api.py**
```python
from src.service.scm_binding_router import create_scm_binding_routes
app.include_router(create_scm_binding_routes(get_current_user=get_current_user, get_db=get_db))
```

- [ ] **Step 5: 跑测试确认通过 + import api 冒烟**（3 passed）。

- [ ] **Step 6: 提交**
```bash
git add src/service/scm_binding_router.py src/service/api.py tests/test_auth/test_scm_binding.py
git commit -m "feat(scm): 工程绑定 + reindex + index-status 端点（入队索引）"
```

---

## Task 7: webhook `POST /webhooks/github`（HMAC→parse→去重→入队）

**Files:** Create `src/service/scm/webhook_verify.py` + `src/service/webhook_router.py`；Modify `src/service/api.py`；Test `tests/test_auth/test_webhook_verify.py` + `test_webhook_router.py`

- [ ] **Step 1: 失败测试（纯函数验签/解析）**
```python
# tests/test_auth/test_webhook_verify.py
import hashlib, hmac
from src.service.scm.webhook_verify import verify_signature, parse_push


def test_verify_signature_ok_and_bad():
    secret, body = "whsec", b'{"a":1}'
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(secret, body, sig) is True
    assert verify_signature(secret, body, "sha256=deadbeef") is False
    assert verify_signature(secret, body, None) is False


def test_parse_push():
    payload = {"ref": "refs/heads/master", "after": "a"*40, "repository": {"id": 42}}
    ev = parse_push(payload)
    assert ev.ref == "master" and ev.after_sha == "a"*40 and ev.repo_external_id == 42
```

- [ ] **Step 2: 跑测试确认失败**。

- [ ] **Step 3: 实现验签/解析**
```python
# src/service/scm/webhook_verify.py
"""GitHub webhook HMAC 验签 + push 解析。设计 §4.4。"""
from __future__ import annotations

import hashlib
import hmac
from typing import Optional

from src.service.scm.base import WebhookEvent


def verify_signature(secret: str, body: bytes, header_sig: Optional[str]) -> bool:
    """常量时间比较 X-Hub-Signature-256（'sha256=...'）。secret 空 / header 缺 → False。"""
    if not secret or not header_sig or not header_sig.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_sig)


def parse_push(payload: dict) -> WebhookEvent:
    """push 事件 → 归一化 WebhookEvent。ref 去 refs/heads/ 前缀。"""
    ref = payload.get("ref", "")
    branch = ref.split("refs/heads/", 1)[-1] if ref.startswith("refs/heads/") else ref
    return WebhookEvent(
        event_type="push", ref=branch, after_sha=payload.get("after"),
        repo_external_id=(payload.get("repository") or {}).get("id"),
    )
```

- [ ] **Step 4: 失败测试（路由）**
```python
# tests/test_auth/test_webhook_router.py
import hashlib, hmac, json
import pytest, pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select
from src.service.db_models_homepage import Base, Project, IndexJob
from src.service.webhook_router import create_webhook_routes


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


def _app(maker, secret="whsec"):
    app = FastAPI()
    async def _get_db():
        async with maker() as s:
            yield s
    app.include_router(create_webhook_routes(get_db=_get_db, webhook_secret=secret))
    return app


def _sig(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.mark.asyncio
async def test_webhook_enqueues_for_bound_branch(maker):
    async with maker() as s:
        s.add(Project(id="p1", name="P1", scm_connection_id="c1", repo_external_id=42,
                      repo_full_name="o/r", ref="master", ref_type="branch")); await s.commit()
    c = TestClient(_app(maker))
    body = json.dumps({"ref": "refs/heads/master", "after": "a"*40, "repository": {"id": 42}}).encode()
    r = c.post("/webhooks/github", content=body,
               headers={"X-Hub-Signature-256": _sig("whsec", body), "X-GitHub-Event": "push",
                        "X-GitHub-Delivery": "d1"})
    assert r.status_code == 200
    async with maker() as s:
        jobs = (await s.execute(select(IndexJob).where(IndexJob.project_id == "p1"))).scalars().all()
        assert len(jobs) == 1 and jobs[0].type == "incremental" and jobs[0].dedup_key == "d1"


def test_webhook_bad_signature_401(maker):
    c = TestClient(_app(maker))
    body = b'{"ref":"refs/heads/master"}'
    r = c.post("/webhooks/github", content=body,
               headers={"X-Hub-Signature-256": "sha256=bad", "X-GitHub-Event": "push", "X-GitHub-Delivery": "d2"})
    assert r.status_code == 401
```

- [ ] **Step 5: 跑测试确认失败**。

- [ ] **Step 6: 实现 webhook 路由**
```python
# src/service/webhook_router.py
"""GitHub webhook 接收：验签→解析→（绑定分支）去重入队→快速 200。设计 §4.4/§9。"""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from src.service.db_models_homepage import Project
from src.service.indexing.queue import enqueue_index_job
from src.service.scm.webhook_verify import verify_signature, parse_push


def create_webhook_routes(*, get_db: Callable, webhook_secret: str) -> APIRouter:
    router = APIRouter(tags=["webhook"])

    @router.post("/webhooks/github")
    async def github_webhook(request: Request, db=Depends(get_db)) -> dict:
        body = await request.body()
        sig = request.headers.get("X-Hub-Signature-256")
        if not verify_signature(webhook_secret, body, sig):
            raise HTTPException(status_code=401, detail="签名校验失败")
        event = request.headers.get("X-GitHub-Event", "")
        delivery = request.headers.get("X-GitHub-Delivery")
        if event != "push":
            return {"ok": True, "ignored": event}     # 仅处理 push（create/installation 后续）
        import json
        ev = parse_push(json.loads(body))
        # 找绑定了该 repo + 该分支的工程
        projects = (await db.execute(
            select(Project).where(Project.repo_external_id == ev.repo_external_id, Project.ref == ev.ref)
        )).scalars().all()
        enqueued = 0
        for p in projects:
            await enqueue_index_job(db, project_id=p.id, type_="incremental",
                                    trigger="webhook", dedup_key=delivery, commit_sha=ev.after_sha)
            enqueued += 1
        await db.commit()
        return {"ok": True, "enqueued": enqueued}      # 快速 200；重活在 worker

    return router
```

- [ ] **Step 7: 挂 api.py**（webhook **不挂 require_infra_healthy / auth**——GitHub 调，用验签鉴权）
```python
import os as _os
from src.service.webhook_router import create_webhook_routes
app.include_router(create_webhook_routes(get_db=get_db, webhook_secret=_os.getenv("KE_GH_WEBHOOK_SECRET", "")))
```

- [ ] **Step 8: 跑测试确认通过**（verify 2 + router 2）+ import api 冒烟。

- [ ] **Step 9: 提交**
```bash
git add src/service/scm/webhook_verify.py src/service/webhook_router.py src/service/api.py tests/test_auth/test_webhook_verify.py tests/test_auth/test_webhook_router.py
git commit -m "feat(scm): POST /webhooks/github（HMAC 验签→解析→去重入队）"
```

---

## Task 8: 接通真实 worker 装配（indexer.py `_main`）

**Files:** Modify `src/service/indexer.py`；Create `src/service/indexing/assembly.py`；Test `tests/test_auth/test_indexer_assembly.py`

> 把"作业→工程→连接→provider→make_real_indexer"装配成一个 `IndexerFn`：对每个 job 按其 project 的绑定造对应 indexer。做成 `build_indexer_for_job(maker, provider, repos_root)` 返回一个"按 job 取绑定再委托 make_real_indexer"的 IndexerFn；`_main` 用它 + 真实 provider + get_session_maker 跑 run_worker_loop。

- [ ] **Step 1: 失败测试**
```python
# tests/test_auth/test_indexer_assembly.py
import pytest, pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from src.service.db_models_homepage import Base, Project, ScmConnection
from src.service.indexing.assembly import build_indexer_for_job


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


class _FakeProvider:
    async def clone(self, installation_id, full_name, ref, subpath, dest):
        assert installation_id == 7 and full_name == "o/r" and ref == "master"
        return "e" * 40


@pytest.mark.asyncio
async def test_build_indexer_resolves_binding_and_clones(maker, tmp_path):
    async with maker() as s:
        s.add(ScmConnection(id="c1", provider="github", auth_type="github_app",
                            github_installation_id=7, account_login="o", status="active"))
        s.add(Project(id="p1", name="P1", scm_connection_id="c1", repo_external_id=42,
                      repo_full_name="o/r", ref="master", ref_type="branch"))
        await s.commit()

    async def fake_run_pipeline(args, cwd=None):
        return ""

    indexer = build_indexer_for_job(maker, provider=_FakeProvider(), repos_root=str(tmp_path),
                                    run_pipeline=fake_run_pipeline)
    class _Job: id="j1"; project_id="p1"
    phases = []
    async def progress(ph, pct): phases.append(ph)
    sha = await indexer(_Job(), progress)
    assert sha == "e" * 40
    assert "cloning" in phases
```

- [ ] **Step 2: 跑测试确认失败**。

- [ ] **Step 3: 实现 assembly**
```python
# src/service/indexing/assembly.py
"""按 job 的工程绑定装配真实 indexer（job→project→scm_connection→provider→make_real_indexer）。设计 §9。"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.service.db_models_homepage import Project, ScmConnection
from src.service.indexing.real_indexer import make_real_indexer, _default_run_pipeline


def build_indexer_for_job(maker: async_sessionmaker, *, provider, repos_root: str,
                          run_pipeline=_default_run_pipeline):
    """返回 IndexerFn：运行时按 job.project_id 解析绑定，再委托 make_real_indexer。"""
    async def _indexer(job, progress) -> str:
        async with maker() as s:
            p = (await s.execute(select(Project).where(Project.id == job.project_id))).scalar_one()
            if not p.scm_connection_id:
                raise RuntimeError(f"工程 {p.id} 未绑定 SCM 连接")
            conn = (await s.execute(
                select(ScmConnection).where(ScmConnection.id == p.scm_connection_id)
            )).scalar_one()
            installation_id = conn.github_installation_id
            full_name, ref, subpath = p.repo_full_name, p.ref, p.subpath
        real = make_real_indexer(
            provider=provider, installation_id=installation_id, full_name=full_name,
            ref=ref, subpath=subpath, repos_root=repos_root, run_pipeline=run_pipeline,
        )
        return await real(job, progress)
    return _indexer
```
（若 `_default_run_pipeline` 是模块私有，real_indexer 顶部已定义；import 它即可。）

- [ ] **Step 4: 跑测试确认通过**。

- [ ] **Step 5: 接通 `_main`**（替换 P2 的 SystemExit 占位）
```python
def _main() -> None:  # pragma: no cover — 进程入口
    import asyncio, os
    from src.service.db import get_session_maker
    from src.service.scm.provider_factory import get_github_provider
    from src.service.indexing.assembly import build_indexer_for_job
    maker = get_session_maker()
    worker_id = os.getenv("KE_INDEXER_WORKER_ID", "ke-indexer-1")
    repos_root = os.getenv("KE_REPOS_ROOT", "/opt/ke-repos")
    indexer = build_indexer_for_job(maker, provider=get_github_provider(), repos_root=repos_root)
    asyncio.run(run_worker_loop(maker, worker_id=worker_id, indexer=indexer))
```

- [ ] **Step 6: P3 全回归 + 提交**
```bash
./venv/bin/python -m pytest tests/test_auth/test_project_binding_model.py tests/test_auth/test_scm_router.py tests/test_auth/test_scm_binding.py tests/test_auth/test_webhook_verify.py tests/test_auth/test_webhook_router.py tests/test_auth/test_indexer_assembly.py -v
./venv/bin/python -c "import src.service.api"   # 冒烟：三 router 挂载不报错
git add src/service/indexer.py src/service/indexing/assembly.py tests/test_auth/test_indexer_assembly.py
git commit -m "feat(indexing): 接通真实 worker 装配（job→绑定→provider→indexer）+ _main"
```

---

## 完成标准（P3 Done）
- 端点：install-url / callback(建连接) / connections(list,delete) / repos / branches / bind / reindex / index-status / webhooks/github。
- `Project` 绑定列 + 迁移（单一 head）。
- webhook HMAC 验签 + push 解析 + 绑定分支去重入队。
- `ke-indexer` `_main` 接通真实 indexer 装配（按 job 绑定 clone+pipeline）。
- 全部单测绿；三 router import 冒烟过。
- **首次端到端可演示**（服务器联调：真实 App + Neo4j/Weaviate）。

## 待后续 Plan 衔接（P4/P5）
- **P4**：OAuth/OIDC 登录 + 身份映射；callback **严格归属核验**；bind/query 的 **SCM-role 门禁 + 按需权限校验**（本计划已留 TODO 挂点）；`/visible-repos` 成员过滤。
- **P5**：加密落盘（`KE_REPOS_ROOT` 加密卷）；密钥 KMS；同步健康度面板（用 index_jobs + last_synced_*）；**卡死作业 reaper**（P2 终审项）。
- state 严格校验（install-url 的 state 存储+回校）；webhook 处理 `installation`(suspend/delete)/`create` 事件。
