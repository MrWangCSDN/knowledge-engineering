# GitHub 连接 P5a — 卡死作业 reaper 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 `index_jobs` 队列加「卡死作业 reaper」—— worker 崩溃后处于 running 阶段的作业不再永久卡死，会被机会式回收（attempts+1，超 MAX→failed 否则→queued）。

**Architecture:** `index_jobs` 加 `lease_expires` 列（迁移）。`claim_next_job` 认领置租约、`run_one_job` 的 progress cb 续租；新 `reclaim_expired_jobs` 回收过期 running 作业（**集合 UPDATE 必须 `synchronize_session=False`** 防 naive<aware TypeError）；`run_one_job` 在 claim 前同 session 机会式 reclaim。`LEASE`=`KE_INDEX_LEASE_SECONDS`（默认 3600）。

**Tech Stack:** SQLAlchemy async / Alembic（batch_alter_table，SQLite 兼容）/ pytest-asyncio / sqlite :memory:。

**设计依据:** Obsidian `GitHub仓库连接-P5a-卡死作业reaper-设计.md`（已过对抗评审实证，修 1 BLOCKER：reclaim UPDATE 必须 `synchronize_session=False`）。

**测试运行约定（必带 env）:**
```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest <路径> -v
```

---

## 文件结构

| 文件 | 责任 | 动作 |
|---|---|---|
| `src/service/db_models_homepage.py` | `IndexJob.lease_expires` 列 | 改 |
| `alembic/versions/index_job_lease_v1.py` | 加 `index_jobs.lease_expires`（batch，down_revision=oauth_identity_v1） | 建 |
| `src/service/indexing/queue.py` | `claim_next_job` 置租约 + 新 `reclaim_expired_jobs`（synchronize_session=False） | 改 |
| `src/service/indexing/runner.py` | `run_one_job` claim 前 reclaim + progress 续租 + lease_seconds 参 | 改 |
| `src/service/indexer.py` | `run_worker_loop`/`_main` 透传 lease_seconds（env） | 改 |
| `tests/test_auth/test_index_job_reaper.py` | reclaim/claim/progress/run_one_job 集成 测试 | 建 |

**关键既有件（已核实 worktree）：**
- `IndexJob`：id/project_id/type/status/trigger/commit_sha/dedup_key/`attempts:int=0`/error/worker_id/progress:JSON/created_at/started_at/finished_at（db_models_homepage.py:248-269）。
- `states.py`：QUEUED/CLONING/BUILDING_GRAPH/CROSS_SERVICE/EMBEDDING/INTERPRETING/DONE/FAILED；PHASE_ORDER；is_terminal()。
- `queue.py`：`enqueue_index_job`、`claim_next_job(session, *, worker_id)`；顶部已 import `select, update` + `datetime, timezone` + `uuid` + `QUEUED, CLONING`。
- `runner.py`：`run_one_job(maker, *, worker_id, indexer)`，`MAX_ATTEMPTS=3`，progress cb 写 job.status/progress+project.indexing_progress；顶部已 import `datetime, timezone` + `select` + `IndexJob, Project` + `claim_next_job` + `DONE, FAILED, QUEUED`。
- `indexer.py`：`run_worker_loop(maker, *, worker_id, indexer, max_rounds=None, idle_sleep=2.0)`；`_main` 读 env。
- **正解范式**：`scm/oauth_state_store.py` 的 `gc_expired` 用 `.execution_options(synchronize_session=False)` 处理同款 SQLite naive<aware 集合 UPDATE。
- alembic head=`oauth_identity_v1`；column-add 迁移用 `op.batch_alter_table`（SQLite 兼容，见 project_scm_binding_v1.py）。

---

## Task 1: `IndexJob.lease_expires` 列 + 迁移

**Files:**
- Modify: `src/service/db_models_homepage.py`
- Create: `alembic/versions/index_job_lease_v1.py`
- Test: `tests/test_auth/test_index_job_reaper.py`（新建，先放列存在性测试）

- [ ] **Step 1: 写失败测试**

新建 `tests/test_auth/test_index_job_reaper.py`：
```python
"""P5a 卡死作业 reaper：lease_expires 列 / reclaim / claim 租约 / progress 续租 / 机会式回收。"""
import pytest
import pytest_asyncio
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from src.service.db_models_homepage import Base, IndexJob, Project


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def _add_project(maker, pid="p1"):
    async with maker() as s:
        s.add(Project(id=pid, name="P"))
        await s.commit()


@pytest.mark.asyncio
async def test_index_job_has_lease_expires_column(maker):
    await _add_project(maker)
    now = datetime.now(timezone.utc)
    async with maker() as s:
        s.add(IndexJob(id="j1", project_id="p1", type="full", status="cloning",
                       trigger="manual", lease_expires=now))
        await s.commit()
    async with maker() as s:
        j = (await s.execute(select(IndexJob).where(IndexJob.id == "j1"))).scalar_one()
        assert j.lease_expires is not None
```

- [ ] **Step 2: 跑红**

```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth/test_index_job_reaper.py -v
```
预期：FAIL（`TypeError: 'lease_expires' is an invalid keyword argument for IndexJob`）。

- [ ] **Step 3: 实现**

(a) `db_models_homepage.py` 的 `IndexJob` 加列（在 `finished_at` 之后）：
```python
    lease_expires: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
```

(b) 新建 `alembic/versions/index_job_lease_v1.py`：
```python
"""index job lease v1: index_jobs 加 lease_expires（卡死作业 reaper 租约）

Revision ID: index_job_lease_v1
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "index_job_lease_v1"
down_revision: Union[str, Sequence[str], None] = "oauth_identity_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("index_jobs") as batch_op:
        batch_op.add_column(sa.Column("lease_expires", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("index_jobs") as batch_op:
        batch_op.drop_column("lease_expires")
```

- [ ] **Step 4: 跑绿 + 单 head 校验**

```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth/test_index_job_reaper.py -v
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/alembic heads
```
预期：1 passed；`alembic heads` 显示**单 head `index_job_lease_v1 (head)`**（若显示多 head 说明 down_revision 错，需修）。

- [ ] **Step 5: Commit**

```bash
git add src/service/db_models_homepage.py alembic/versions/index_job_lease_v1.py tests/test_auth/test_index_job_reaper.py
git commit -m "feat(indexing): index_jobs 加 lease_expires 列 + 迁移（P5a T1）"
```

---

## Task 2: `reclaim_expired_jobs` + `claim_next_job` 置租约

**Files:**
- Modify: `src/service/indexing/queue.py`
- Test: `tests/test_auth/test_index_job_reaper.py`（追加 reclaim/claim 测试）

### 背景
`reclaim_expired_jobs` 回收**过期 running**（status 非 queued 非终态）作业：`lease_expires<now` 或（`lease_expires IS NULL` 且 `started_at<now-LEASE`，兜底旧行）。attempts+1 后 ≥MAX→FAILED 否则→QUEUED。**两条集合 UPDATE 必须 `.execution_options(synchronize_session=False)`**（否则默认 "evaluate" 在已加载 ORM 对象上跑 naive<aware 抛 TypeError——评审实证 BLOCKER，范式同 `oauth_state_store.gc_expired`）。`claim_next_job` 认领时置 `lease_expires=now+LEASE`。

- [ ] **Step 1: 写失败测试**（追加）

```python
from src.service.indexing.queue import reclaim_expired_jobs, claim_next_job
from src.service.indexing.runner import MAX_ATTEMPTS


async def _add_job(maker, *, jid, status, attempts=0, lease_delta=None, started_delta=None,
                   project_id="p1"):
    """lease_delta/started_delta 单位秒，相对 now（负=过去）；None=该字段不设(NULL)。"""
    now = datetime.now(timezone.utc)
    async with maker() as s:
        s.add(IndexJob(
            id=jid, project_id=project_id, type="full", status=status, trigger="manual",
            attempts=attempts,
            lease_expires=(now + timedelta(seconds=lease_delta)) if lease_delta is not None else None,
            started_at=(now + timedelta(seconds=started_delta)) if started_delta is not None else None,
        ))
        await s.commit()


async def _get(maker, jid):
    async with maker() as s:
        return (await s.execute(select(IndexJob).where(IndexJob.id == jid))).scalar_one()


@pytest.mark.asyncio
async def test_reclaim_expired_running_to_queued(maker):
    await _add_project(maker)
    await _add_job(maker, jid="j1", status="cloning", attempts=0, lease_delta=-10)
    async with maker() as s:
        n = await reclaim_expired_jobs(s, lease_seconds=3600)
        await s.commit()
    assert n == 1
    j = await _get(maker, "j1")
    assert j.status == "queued" and j.attempts == 1 and j.worker_id is None and j.lease_expires is None


@pytest.mark.asyncio
async def test_reclaim_at_max_to_failed(maker):
    await _add_project(maker)
    await _add_job(maker, jid="j2", status="embedding", attempts=MAX_ATTEMPTS - 1, lease_delta=-10)
    async with maker() as s:
        n = await reclaim_expired_jobs(s, lease_seconds=3600)
        await s.commit()
    assert n == 1
    j = await _get(maker, "j2")
    assert j.status == "failed" and j.attempts == MAX_ATTEMPTS
    assert j.finished_at is not None and "reaper" in (j.error or "")


@pytest.mark.asyncio
async def test_reclaim_skips_unexpired_and_terminal_and_queued(maker):
    await _add_project(maker)
    await _add_job(maker, jid="fresh", status="cloning", lease_delta=+3600)   # 未过期
    await _add_job(maker, jid="qd", status="queued", lease_delta=-10)          # queued 非 running
    await _add_job(maker, jid="dn", status="done", lease_delta=-10)            # 终态
    await _add_job(maker, jid="fl", status="failed", lease_delta=-10)          # 终态
    async with maker() as s:
        n = await reclaim_expired_jobs(s, lease_seconds=3600)
        await s.commit()
    assert n == 0
    assert (await _get(maker, "fresh")).status == "cloning"
    assert (await _get(maker, "qd")).status == "queued"


@pytest.mark.asyncio
async def test_reclaim_null_lease_uses_started_at(maker):
    await _add_project(maker)
    await _add_job(maker, jid="old", status="cloning", lease_delta=None, started_delta=-7200)  # NULL lease, 老
    await _add_job(maker, jid="new", status="cloning", lease_delta=None, started_delta=-10)    # NULL lease, 新
    async with maker() as s:
        n = await reclaim_expired_jobs(s, lease_seconds=3600)
        await s.commit()
    assert n == 1
    assert (await _get(maker, "old")).status == "queued"
    assert (await _get(maker, "new")).status == "cloning"


@pytest.mark.asyncio
async def test_claim_sets_lease(maker):
    await _add_project(maker)
    await _add_job(maker, jid="q1", status="queued")
    async with maker() as s:
        job = await claim_next_job(s, worker_id="w1", lease_seconds=1800)
        await s.commit()
    assert job is not None and job.lease_expires is not None
    now = datetime.now(timezone.utc)
    lease = job.lease_expires
    if lease.tzinfo is None:
        lease = lease.replace(tzinfo=timezone.utc)
    assert lease > now + timedelta(seconds=1500)   # ≈ now+1800，宽容差
```

- [ ] **Step 2: 跑红**

```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth/test_index_job_reaper.py -v
```
预期：FAIL（`ImportError: cannot import name 'reclaim_expired_jobs'`；claim_next_job 未收 lease_seconds）。

- [ ] **Step 3: 实现**

`src/service/indexing/queue.py`：
(a) 顶部 import 补：`from datetime import datetime, timezone, timedelta`（加 timedelta）；`from sqlalchemy import select, update, and_, or_`（加 and_, or_）；`from src.service.indexing.states import QUEUED, CLONING, DONE, FAILED`（加 DONE, FAILED）。

(b) 改 `claim_next_job` 签名 + UPDATE 置租约：
```python
async def claim_next_job(session, *, worker_id, lease_seconds=3600):
    row = (await session.execute(
        select(IndexJob.id).where(IndexJob.status == QUEUED).order_by(IndexJob.created_at).limit(1)
    )).scalars().first()
    if row is None:
        return None
    now = datetime.now(timezone.utc)
    res = await session.execute(
        update(IndexJob).where(IndexJob.id == row, IndexJob.status == QUEUED)
        .values(status=CLONING, worker_id=worker_id, started_at=now,
                lease_expires=now + timedelta(seconds=lease_seconds))
    )
    await session.flush()
    if res.rowcount != 1:
        return None
    return (await session.execute(select(IndexJob).where(IndexJob.id == row))).scalar_one()
```

(c) 新增 `reclaim_expired_jobs`：
```python
async def reclaim_expired_jobs(session, *, lease_seconds=3600, now=None) -> int:
    """回收过期租约的 running 作业（机会式，claim 前调）。返回回收条数。
    running = 非 queued 非终态；过期 = lease_expires<now 或（lease_expires IS NULL 且 started_at<now-LEASE）。
    attempts+1 后 >=MAX_ATTEMPTS → FAILED；否则 → QUEUED。两条集合 UPDATE 必须 synchronize_session=False
    （否则默认 evaluate 在已加载 ORM 对象上跑 naive<aware 抛 TypeError，范式同 oauth_state_store.gc_expired）。"""
    from src.service.indexing.runner import MAX_ATTEMPTS   # 延迟 import 防 runner↔queue 环
    if now is None:
        now = datetime.now(timezone.utc)
    started_cutoff = now - timedelta(seconds=lease_seconds)
    running = IndexJob.status.notin_([QUEUED, DONE, FAILED])
    expired = or_(IndexJob.lease_expires < now,
                  and_(IndexJob.lease_expires.is_(None), IndexJob.started_at < started_cutoff))
    base = and_(running, expired)
    r_fail = await session.execute(
        update(IndexJob).where(base, IndexJob.attempts + 1 >= MAX_ATTEMPTS).values(
            status=FAILED, attempts=IndexJob.attempts + 1, finished_at=now, worker_id=None,
            error="reclaimed by reaper: lease expired (max attempts)")
        .execution_options(synchronize_session=False))
    r_queue = await session.execute(
        update(IndexJob).where(base, IndexJob.attempts + 1 < MAX_ATTEMPTS).values(
            status=QUEUED, attempts=IndexJob.attempts + 1, worker_id=None, lease_expires=None)
        .execution_options(synchronize_session=False))
    await session.flush()
    return (r_fail.rowcount or 0) + (r_queue.rowcount or 0)
```

- [ ] **Step 4: 跑绿**

```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth/test_index_job_reaper.py -v
```
预期：T1(1) + T2(5) 全绿。

- [ ] **Step 5: Commit**

```bash
git add src/service/indexing/queue.py tests/test_auth/test_index_job_reaper.py
git commit -m "feat(indexing): reclaim_expired_jobs + claim 置租约（synchronize_session=False）（P5a T2）"
```

---

## Task 3: runner 续租 + claim 前 reclaim + indexer 透传

**Files:**
- Modify: `src/service/indexing/runner.py`、`src/service/indexer.py`
- Test: `tests/test_auth/test_index_job_reaper.py`（追加机会式集成测试，B1 回归门）

- [ ] **Step 1: 写失败测试**（追加）

```python
from src.service.indexer import run_worker_loop


@pytest.mark.asyncio
async def test_run_one_job_reclaims_before_claim(maker):
    """B1 回归门 + 机会式：seed 过期 running + 1 queued → 一轮 run_one_job：过期者回 queued 并在同轮被认领。
    （reclaim+claim 同 session，且会 load IndexJob 对象——若 reclaim 缺 synchronize_session=False 此处必抛 TypeError）。"""
    from src.service.indexing.runner import run_one_job
    await _add_project(maker)
    # 过期 running（更老，created/started 早）+ 一个 queued
    await _add_job(maker, jid="stuck", status="cloning", lease_delta=-10, started_delta=-7200)
    await _add_job(maker, jid="fresh_q", status="queued")
    seen = {}
    async def _indexer(job, progress):
        seen["job_id"] = job.id
        await progress("cloning", 10)
        return "deadbeef"
    handled = await run_one_job(maker, worker_id="w1", indexer=_indexer, lease_seconds=3600)
    assert handled is True
    # stuck 被回收→queued 后，按 created_at 最早优先被本轮认领处理
    assert seen["job_id"] == "stuck"
    j = await _get(maker, "stuck")
    assert j.status == "done" and j.commit_sha == "deadbeef"


@pytest.mark.asyncio
async def test_progress_extends_lease(maker):
    """progress cb 续租：claim 后跑 progress，lease_expires 被推后。"""
    from src.service.indexing.runner import run_one_job
    await _add_project(maker)
    await _add_job(maker, jid="q1", status="queued")
    captured = {}
    async def _indexer(job, progress):
        await progress("embedding", 50)
        async with maker() as s:   # 读续租后的 lease
            j = (await s.execute(select(IndexJob).where(IndexJob.id == job.id))).scalar_one()
            captured["lease"] = j.lease_expires
        return "c0ffee"
    await run_one_job(maker, worker_id="w1", indexer=_indexer, lease_seconds=1800)
    assert captured["lease"] is not None
    now = datetime.now(timezone.utc)
    lease = captured["lease"]
    if lease.tzinfo is None:
        lease = lease.replace(tzinfo=timezone.utc)
    assert lease > now + timedelta(seconds=1500)


@pytest.mark.asyncio
async def test_run_worker_loop_passes_lease_seconds(maker):
    """run_worker_loop 透传 lease_seconds 到 run_one_job（不抛、能处理）。"""
    await _add_project(maker)
    await _add_job(maker, jid="q1", status="queued")
    async def _indexer(job, progress):
        await progress("cloning", 5)
        return "abc1234"
    processed = await run_worker_loop(maker, worker_id="w1", indexer=_indexer,
                                      max_rounds=2, lease_seconds=900)
    assert processed == 1
    assert (await _get(maker, "q1")).status == "done"
```

- [ ] **Step 2: 跑红**

```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth/test_index_job_reaper.py -k "reclaims_before_claim or extends_lease or passes_lease" -v
```
预期：FAIL（run_one_job/run_worker_loop 未收 lease_seconds；run_one_job 未在 claim 前 reclaim）。

- [ ] **Step 3: 实现**

(a) `src/service/indexing/runner.py`：
- 顶部 import 补 `timedelta`：`from datetime import datetime, timezone, timedelta`；`from src.service.indexing.queue import claim_next_job, reclaim_expired_jobs`（加 reclaim_expired_jobs）。
- `run_one_job` 签名加 `lease_seconds: int = 3600`；claim 前 reclaim（同 session）：
```python
async def run_one_job(maker, *, worker_id, indexer, lease_seconds=3600) -> bool:
    async with maker() as s:
        await reclaim_expired_jobs(s, lease_seconds=lease_seconds)      # 机会式自愈
        job = await claim_next_job(s, worker_id=worker_id, lease_seconds=lease_seconds)
        await s.commit()
    if job is None:
        return False
    ...
```
- progress cb 续租（在写 j.progress 后加一行）：
```python
            j.lease_expires = datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
```
（其余 run_one_job 逻辑不动。）

(b) `src/service/indexer.py`：
- `run_worker_loop` 签名加 `lease_seconds: int = 3600`，透传给 `run_one_job`：
```python
async def run_worker_loop(maker, *, worker_id, indexer, max_rounds=None, idle_sleep=2.0,
                          lease_seconds=3600) -> int:
    ...
        handled = await run_one_job(maker, worker_id=worker_id, indexer=indexer, lease_seconds=lease_seconds)
    ...
```
- `_main` 读 env 传入：
```python
    lease_seconds = int(os.getenv("KE_INDEX_LEASE_SECONDS", "3600"))
    asyncio.run(run_worker_loop(maker, worker_id=worker_id, indexer=indexer, lease_seconds=lease_seconds))
```

- [ ] **Step 4: 跑绿（全文件不回归）**

```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth/test_index_job_reaper.py -v
```
预期：T1(1)+T2(5)+T3(3)=9 全绿。

- [ ] **Step 5: import 冒烟 + 既有索引测试 + 全量回归**

```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -c "import src.service.indexer; import src.service.api; print('import ok')"
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth -q
```
预期：import ok；全量绿（P4c 基线 1086 passed + 本片新增 9；既有 index runner/queue 测试无回归）。贴最终 passed 数。

- [ ] **Step 6: Commit**

```bash
git add src/service/indexing/runner.py src/service/indexer.py tests/test_auth/test_index_job_reaper.py
git commit -m "feat(indexing): run_one_job claim 前 reclaim + progress 续租 + lease_seconds 透传（P5a T3）"
```

---

## 验收标准（P5a Done）

1. **迁移**：index_jobs 有 lease_expires 列；`alembic heads` 单 head `index_job_lease_v1`。
2. **认领置租约**：claim_next_job 置 lease_expires=now+LEASE。
3. **续租**：progress cb 写续 lease。
4. **回收**：过期 running→queued(attempts+1)/超 MAX→failed(finished_at+error)；未过期/queued/终态不动；NULL 租约用 started_at 兜底；返回回收条数。
5. **机会式 + B1**：run_one_job claim 前同 session reclaim（已加载 ORM 对象下不抛 TypeError），回收者本轮可被认领。
6. **配置**：run_worker_loop/_main 透传 KE_INDEX_LEASE_SECONDS。
7. **回归**：全量 test_auth 绿 + import 冒烟。

## 自审记录（writing-plans self-review）

- **Spec 覆盖**：列+迁移→T1；reclaim+claim 租约→T2；progress 续租+claim 前 reclaim+透传→T3；§6 全部用例（reclaim 五态、claim 置租、progress 续、机会式集成、透传）映射到 T1-T3。✅
- **类型一致**：IndexJob/states/queue/runner/indexer 签名与字段按 worktree 实际核对；`MAX_ATTEMPTS=3` 延迟 import；migration down_revision=oauth_identity_v1（实证单 head）；batch_alter_table（SQLite 兼容）。✅
- **占位符**：无 TBD。✅
- **风险点**：(a) **B1 必做**——reclaim 两条 UPDATE 的 `.execution_options(synchronize_session=False)` 不可省（test_run_one_job_reclaims_before_claim 是回归门，它会 load IndexJob 对象触发）；(b) NULL 租约靠 started_at 兜底；(c) 延迟 import MAX_ATTEMPTS 防 runner↔queue 环；(d) SQLite naive 时间断言用 `replace(tzinfo=utc)` 兜底后比较。
