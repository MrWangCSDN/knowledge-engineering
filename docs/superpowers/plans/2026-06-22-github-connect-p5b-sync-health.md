# GitHub 连接 P5b — 同步健康度面板 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增只读端点 `GET /projects/{id}/sync-health` —— 返回工程同步状态 + staleness + is_stuck（P5a 租约信号）+ 按 project 聚合 index_jobs 计数 + latest_job + last_error，供运维/用户自助排障。

**Architecture:** 单端点加进 `scm_binding_router.py`（与 index-status 同 router/工厂，复用注入式 `require_role("reporter")`）。**纯 DB 读、无新列/迁移/kill-switch**。5 个轻量 SELECT。is_stuck 镜像 P5a reclaim 的「过期 running」谓词（inline，不回改 reclaim）。

**Tech Stack:** FastAPI / SQLAlchemy async / pytest-asyncio / sqlite :memory:。

**分支:** `feat/sync-health-p5b`（栈式，base=`feat/github-repo-connect`；PR #2 冻结在 P5a）。完成后开**栈式 PR**（base=feat/github-repo-connect，diff 只含 P5b）。

**设计依据:** Obsidian `GitHub仓库连接-P5b-同步健康度面板-设计.md`（已过对抗评审实证；修 I-1 created_at 秒级排序→latest_job 加 desc(id) tie-breaker + 测试 seed 拉开 created_at）。

**测试运行约定（必带 env）:**
```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest <路径> -v
```

---

## 文件结构

| 文件 | 责任 | 动作 |
|---|---|---|
| `src/service/scm_binding_router.py` | `_iso` 模块级工具 + `GET /projects/{id}/sync-health` 端点 + 补 import | 改 |
| `tests/test_auth/test_sync_health.py` | 端点测试矩阵 | 建 |

**关键既有件（已核实 worktree）：**
- `create_scm_binding_routes(*, get_current_user, get_db, require_role=require_project_role, authorize_scm=None)`（scm_binding_router.py:33-40）；`index-status` 端点 `dependencies=[Depends(require_role("reporter"))]`（:98-99）。
- 现有 import：`from sqlalchemy import select, desc`、`from fastapi import APIRouter, Depends, HTTPException`、`Project, IndexJob, ScmConnection`。
- `Project`：status/last_synced_at/last_synced_commit。`IndexJob`：status/error/created_at(`CURRENT_TIMESTAMP` 秒级)/started_at/finished_at(runner `datetime.now()` 微秒级)/progress/lease_expires。
- `states.py`：`QUEUED/DONE/FAILED`、`PHASE_ORDER=[cloning,building_graph,cross_service,embedding,interpreting]`。
- 测试范式：no-op require_role 注入 = `require_role=lambda role: (lambda: None)`（test_scm_binding.py:30）；403 真 RBAC 见 test_scm_binding_rbac.py。
- `KE_INDEX_LEASE_SECONDS` 默认 3600（P5a）。

---

## Task 1: `sync-health` 端点（含 _iso + imports）+ 测试矩阵

**Files:**
- Modify: `src/service/scm_binding_router.py`
- Test: `tests/test_auth/test_sync_health.py`（新建）

- [ ] **Step 1: 写失败测试**

新建 `tests/test_auth/test_sync_health.py`：
```python
"""P5b 同步健康度面板 GET /projects/{id}/sync-health。"""
import pytest
import pytest_asyncio
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
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


def _app(maker, *, require_role=None):
    app = FastAPI()
    async def _get_db():
        async with maker() as s:
            yield s
    app.include_router(create_scm_binding_routes(
        get_current_user=lambda: _User(), get_db=_get_db,
        require_role=require_role or (lambda role: (lambda: None)),  # no-op 放行
    ))
    return app


_BASE = datetime(2026, 6, 22, 10, 0, 0, tzinfo=timezone.utc)


async def _add_project(maker, *, pid="p1", status="indexing", last_synced_at=None, last_synced_commit=None):
    async with maker() as s:
        s.add(Project(id=pid, name="P", status=status,
                      last_synced_at=last_synced_at, last_synced_commit=last_synced_commit))
        await s.commit()


async def _add_job(maker, *, jid, status, project_id="p1", error=None, created_at=None,
                   finished_at=None, started_at=None, lease_expires=None):
    async with maker() as s:
        s.add(IndexJob(id=jid, project_id=project_id, type="full", status=status, trigger="manual",
                       error=error, created_at=created_at, finished_at=finished_at,
                       started_at=started_at, lease_expires=lease_expires))
        await s.commit()


@pytest.mark.asyncio
async def test_sync_health_404_when_project_missing(maker):
    c = TestClient(_app(maker))
    assert c.get("/projects/nope/sync-health").status_code == 404


@pytest.mark.asyncio
async def test_sync_health_reporter_gate_denied(maker):
    await _add_project(maker)
    def _deny(role):
        def _dep():
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail="no")
        return _dep
    c = TestClient(_app(maker, require_role=_deny))
    assert c.get("/projects/p1/sync-health").status_code == 403


@pytest.mark.asyncio
async def test_sync_health_rich(maker):
    await _add_project(maker, status="indexing",
                       last_synced_at=_BASE - timedelta(hours=24), last_synced_commit="abc123")
    # 2 queued / 1 cloning(running) / 3 failed；created_at 显式拉开（I-1：秒级分辨率）
    await _add_job(maker, jid="q1", status="queued", created_at=_BASE - timedelta(hours=5))
    await _add_job(maker, jid="q2", status="queued", created_at=_BASE - timedelta(hours=4))
    await _add_job(maker, jid="run1", status="cloning", created_at=_BASE - timedelta(hours=3))
    await _add_job(maker, jid="f1", status="failed", error="boom-old",
                   created_at=_BASE - timedelta(hours=6), finished_at=_BASE - timedelta(hours=6))
    await _add_job(maker, jid="f2", status="failed", error="boom-mid",
                   created_at=_BASE - timedelta(hours=2), finished_at=_BASE - timedelta(hours=2))
    await _add_job(maker, jid="f3", status="failed", error="boom-new",
                   created_at=_BASE - timedelta(hours=1), finished_at=_BASE - timedelta(minutes=30))
    c = TestClient(_app(maker))
    body = c.get("/projects/p1/sync-health").json()
    assert body["status"] == "indexing"
    assert body["last_synced_commit"] == "abc123"
    assert body["job_counts"] == {"queued": 2, "running": 1, "failed": 3}
    assert body["latest_job"]["job_id"] == "f3"          # created_at 最近
    assert body["last_error"] == "boom-new"               # finished_at 最近的 failed
    assert body["staleness_hours"] is not None and body["staleness_hours"] >= 23


@pytest.mark.asyncio
async def test_sync_health_never_synced_and_no_jobs(maker):
    await _add_project(maker, status="configured", last_synced_at=None)
    c = TestClient(_app(maker))
    body = c.get("/projects/p1/sync-health").json()
    assert body["staleness_hours"] is None
    assert body["job_counts"] == {"queued": 0, "running": 0, "failed": 0}
    assert body["latest_job"] is None and body["last_error"] is None
    assert body["is_stuck"] is False


@pytest.mark.asyncio
async def test_sync_health_is_stuck_true_when_lease_expired(maker):
    await _add_project(maker)
    now = datetime.now(timezone.utc)
    await _add_job(maker, jid="stuck", status="embedding",
                   lease_expires=now - timedelta(seconds=10))
    c = TestClient(_app(maker))
    assert c.get("/projects/p1/sync-health").json()["is_stuck"] is True


@pytest.mark.asyncio
async def test_sync_health_is_stuck_false_when_lease_future(maker):
    await _add_project(maker)
    now = datetime.now(timezone.utc)
    await _add_job(maker, jid="ok", status="embedding",
                   lease_expires=now + timedelta(hours=1))
    c = TestClient(_app(maker))
    assert c.get("/projects/p1/sync-health").json()["is_stuck"] is False


@pytest.mark.asyncio
async def test_sync_health_is_stuck_null_lease_started_fallback(maker):
    await _add_project(maker)
    now = datetime.now(timezone.utc)
    await _add_job(maker, jid="old", status="cloning", lease_expires=None,
                   started_at=now - timedelta(seconds=7200))
    c = TestClient(_app(maker))
    assert c.get("/projects/p1/sync-health").json()["is_stuck"] is True
```

- [ ] **Step 2: 跑红**

```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth/test_sync_health.py -v
```
预期：FAIL（404 那条可能因路由不存在也返 404 假绿；其余 rich/stuck 断言因端点不存在 FAIL）。

- [ ] **Step 3: 实现（`src/service/scm_binding_router.py`）**

(a) 顶部 import 补：
- `from sqlalchemy import select, desc, func, and_, or_`（加 func, and_, or_）
- `import os`（文件顶部，与其他 stdlib import 一起）
- `from datetime import datetime, timezone, timedelta`
- `from src.service.indexing.states import QUEUED, DONE, FAILED, PHASE_ORDER`

(b) 在 `create_scm_binding_routes` **之上**加模块级工具：
```python
def _iso(dt):
    """datetime → ISO 字符串；None 透传。"""
    return dt.isoformat() if dt is not None else None
```

(c) 在 `create_scm_binding_routes` 内（`index-status` 端点之后、`return router` 之前）加端点：
```python
    @router.get("/projects/{project_id}/sync-health",
                dependencies=[Depends(require_role("reporter"))])
    async def sync_health(project_id: str, user=Depends(get_current_user), db=Depends(get_db)) -> dict:
        proj = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()
        if proj is None:
            raise HTTPException(status_code=404, detail="工程不存在")
        now = datetime.now(timezone.utc)
        lease_seconds = int(os.getenv("KE_INDEX_LEASE_SECONDS", "3600"))
        # 1) 最近一条 job（created_at 秒级，加 id tie-breaker 保确定性）
        latest = (await db.execute(
            select(IndexJob).where(IndexJob.project_id == project_id)
            .order_by(desc(IndexJob.created_at), desc(IndexJob.id)).limit(1))).scalars().first()
        latest_job = None if latest is None else {
            "job_id": latest.id, "status": latest.status, "progress": latest.progress,
            "error": latest.error, "finished_at": _iso(latest.finished_at)}
        # 2) 按 status 聚合 → 三桶
        rows = (await db.execute(
            select(IndexJob.status, func.count()).where(IndexJob.project_id == project_id)
            .group_by(IndexJob.status))).all()
        counts = {st: n for st, n in rows}
        job_counts = {
            "queued": counts.get(QUEUED, 0),
            "running": sum(counts.get(p, 0) for p in PHASE_ORDER),
            "failed": counts.get(FAILED, 0)}
        # 3) 最近一条 failed 的 error（finished_at 微秒级，确定）
        last_error = (await db.execute(
            select(IndexJob.error).where(IndexJob.project_id == project_id, IndexJob.status == FAILED)
            .order_by(desc(IndexJob.finished_at)).limit(1))).scalars().first()
        # 4) is_stuck：存在 running 阶段且租约过期（镜像 P5a reclaim 谓词；不回改 reclaim）
        started_cutoff = now - timedelta(seconds=lease_seconds)
        running = IndexJob.status.notin_([QUEUED, DONE, FAILED])
        expired = or_(IndexJob.lease_expires < now,
                      and_(IndexJob.lease_expires.is_(None), IndexJob.started_at < started_cutoff))
        stuck_id = (await db.execute(
            select(IndexJob.id).where(IndexJob.project_id == project_id, running, expired).limit(1)
        )).scalars().first()
        # 5) staleness（SQLite naive 兜底）
        ls = proj.last_synced_at
        if ls is not None and ls.tzinfo is None:
            ls = ls.replace(tzinfo=timezone.utc)
        staleness_hours = None if ls is None else round((now - ls).total_seconds() / 3600, 2)
        return {
            "project_id": project_id, "status": proj.status,
            "last_synced_at": _iso(proj.last_synced_at), "last_synced_commit": proj.last_synced_commit,
            "staleness_hours": staleness_hours, "is_stuck": stuck_id is not None,
            "latest_job": latest_job, "job_counts": job_counts, "last_error": last_error}
```

- [ ] **Step 4: 跑绿**

```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth/test_sync_health.py -v
```
预期：7 passed。

- [ ] **Step 5: import 冒烟 + 全量回归**

```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -c "import src.service.api; print('import ok')"
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth -q
```
预期：import ok；全量绿（P5a 基线 1095 passed + 本片新增 7 = 约 1102）。**贴最终 passed 数**；既有 scm_binding 测试不回归。

- [ ] **Step 6: 自审 + commit**

```bash
git add src/service/scm_binding_router.py tests/test_auth/test_sync_health.py
git commit -m "feat(scm): GET /projects/{id}/sync-health 同步健康度面板（P5b）"
```
**只 add 这两个文件**；plan/venv 不要 add。

---

## 验收标准（P5b Done）
1. 不存在工程→404；reporter 门生效（no-op 放行 / deny→403）。
2. 响应 9 字段齐：project_id/status/last_synced_at/last_synced_commit/staleness_hours/is_stuck/latest_job/job_counts/last_error。
3. job_counts 三桶正确（running=PHASE_ORDER 之和、done 不计入）；latest_job=最近 created（id tie-breaker）；last_error=最近 finished 的 failed。
4. staleness_hours=距 last_synced_at 小时（None→null，naive 兜底）。
5. is_stuck=running+租约过期（含 NULL 租约 started_at 兜底）。
6. 纯只读不写库；全量 test_auth 绿 + import 冒烟。

## 自审记录（writing-plans self-review）
- **Spec 覆盖**：端点 + _iso + imports + 测试矩阵（404/reporter 门/rich/never-synced+no-jobs/is_stuck 三态）全映射到 Task 1。✅
- **类型一致**：create_scm_binding_routes/require_role 注入、Project/IndexJob 字段、states PHASE_ORDER、func/and_/or_ 用法按 worktree 实际核对；I-1 tie-breaker(desc(id))；staleness naive 兜底；is_stuck 谓词镜像 P5a reclaim。✅
- **占位符**：无 TBD。✅
- **风险点**：(a) I-1 测试 seed 显式拉开 created_at（已在 rich 测试做）；(b) is_stuck 用只读 SELECT（aware now vs naive 列），无 P5a 的 UPDATE/synchronize_session 问题（评审实证）；(c) 既有 scm_binding 测试靠 no-op require_role 注入不回归。
