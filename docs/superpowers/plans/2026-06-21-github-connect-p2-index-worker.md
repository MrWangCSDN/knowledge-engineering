# GitHub 连接 P2：索引作业系统 + ke-indexer worker

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans，逐任务执行。Steps 用 `- [ ]` 复选框。

**Goal:** 让 KE 能把"索引一个仓库"作为可续/可重试/可观测的后台作业跑起来：`index_jobs` 队列表 + 独立 `ke-indexer` worker 进程（认领作业→clone→跑 pipeline→逐阶段写进度/状态→done/failed/重试）。

**Architecture:** DB 作业队列（`index_jobs`）解耦 API 与重活。Worker 是独立可 `python -m src.service.indexer` 启动的进程，用 `db.get_session_maker()` 拿 session，原子认领 queued 作业，调一个**可注入的 indexer 回调**（默认实现 = P1 的 `GitHubAppProvider.clone` + shell 出 `src.pipeline.cli`），通过 progress 回调把阶段写进 `index_job.progress` + `project.status/indexing_progress`。失败指数退避重试。编排逻辑全部用 fake indexer 单测；真实索引走 gated 集成。

**Tech Stack:** Python 3.12 / SQLAlchemy async / Alembic / asyncio / pytest(+asyncio)。复用 P1 `GitHubAppProvider`、现有 `src/pipeline/cli.py`、`src/service/db.py`。

**设计依据:** Obsidian `GitHub仓库连接-设计.md` §5.3/§9、`身份与授权模型-设计.md`（无关）。**前置:** P1 已合入本分支。worktree `/Users/java/ke-github-connect`，测试 `./venv/bin/python -m pytest`（import 报 KE_JWT_SECRET/KE_TOKEN_ENC_KEY 时按 P1 方式加 env）。

---

## 文件结构

| 文件 | 责任 |
|---|---|
| `src/service/db_models_homepage.py`（改） | 新增 `IndexJob` 模型 |
| `alembic/versions/<id>_index_jobs.py` | 建 `index_jobs` 表 |
| `src/service/indexing/__init__.py` | 包入口 |
| `src/service/indexing/queue.py` | `enqueue_index_job` + `claim_next_job`（原子认领） |
| `src/service/indexing/states.py` | 阶段常量 + 合法转移 |
| `src/service/indexing/runner.py` | `run_one_job`（编排：认领→indexer→进度→done/failed/retry）+ `IndexerFn` 类型 + `ProgressCb` |
| `src/service/indexing/real_indexer.py` | 默认 indexer：clone(P1) + shell `src.pipeline.cli`（gated 集成） |
| `src/service/indexer.py` | worker 循环 + `python -m src.service.indexer` 入口 |
| `deploy/ke-indexer.service` | systemd 单元（运维文档，不测） |
| `tests/test_auth/test_index_job_model.py` / `test_index_queue.py` / `test_index_states.py` / `test_index_runner.py` / `test_indexer_loop.py` | 测试 |

---

## Task 1: `IndexJob` 模型 + 迁移

**Files:** Modify `src/service/db_models_homepage.py`；Create `alembic/versions/index_jobs_v1_index_jobs.py`；Test `tests/test_auth/test_index_job_model.py`

- [ ] **Step 1: 失败测试**
```python
# tests/test_auth/test_index_job_model.py
"""index_jobs 作业队列模型测试（in-memory SQLite）。"""
import pytest, pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select
from src.service.db_models_homepage import Base, IndexJob


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s


@pytest.mark.asyncio
async def test_index_job_roundtrip(session):
    j = IndexJob(id="job-1", project_id="proj-1", type="full_index",
                 status="queued", trigger="manual", dedup_key="d-1")
    session.add(j); await session.commit()
    row = (await session.execute(select(IndexJob).where(IndexJob.id == "job-1"))).scalar_one()
    assert row.status == "queued"
    assert row.type == "full_index"
    assert row.trigger == "manual"
    assert row.attempts == 0          # 默认 0
    assert row.progress is None
```

- [ ] **Step 2: 跑测试确认失败** — `./venv/bin/python -m pytest tests/test_auth/test_index_job_model.py -v` → ImportError IndexJob。

- [ ] **Step 3: 加模型**（`db_models_homepage.py`，`ScmConnection` 之后；复用现有导入风格，`JSON` 若未导入则补到 `from sqlalchemy import ...`）
```python
class IndexJob(Base):
    """索引作业队列（设计 §5.3/§9）。worker 原子认领 queued → 逐阶段更新 → done/failed。"""
    __tablename__ = "index_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    # full_index / incremental / reindex
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    # queued / cloning / building_graph / cross_service / embedding / interpreting / done / failed
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    # manual / webhook / schedule
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    commit_sha: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    # webhook X-GitHub-Delivery 去重
    dedup_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    worker_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # {phase, percent, eta_seconds}
    progress: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=text("(CURRENT_TIMESTAMP)"), nullable=False
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
```

- [ ] **Step 4: 跑测试确认通过**（1 passed）。

- [ ] **Step 5: 迁移**（先 `./venv/bin/alembic heads` 取当前 head 作 down_revision——P1 后应为 `scm_connection_v1`；若不同用实际值并报告）
```python
# alembic/versions/index_jobs_v1_index_jobs.py
"""index jobs v1: index_jobs 表

Revision ID: index_jobs_v1
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "index_jobs_v1"
down_revision: Union[str, Sequence[str], None] = "scm_connection_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "index_jobs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("trigger", sa.String(length=32), nullable=False),
        sa.Column("commit_sha", sa.String(length=40), nullable=True),
        sa.Column("dedup_key", sa.String(length=128), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("worker_id", sa.String(length=64), nullable=True),
        sa.Column("progress", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.Index("idx_index_jobs_status", "status"),
    )


def downgrade() -> None:
    op.drop_table("index_jobs")
```
确认 `./venv/bin/alembic heads` 单一 head = `index_jobs_v1`。

- [ ] **Step 6: 提交**
```bash
git add src/service/db_models_homepage.py alembic/versions/index_jobs_v1_index_jobs.py tests/test_auth/test_index_job_model.py
git commit -m "feat(indexing): index_jobs 作业队列模型 + 迁移"
```

---

## Task 2: 阶段常量 + 合法转移（states.py）

**Files:** Create `src/service/indexing/__init__.py`(空) + `src/service/indexing/states.py`；Test `tests/test_auth/test_index_states.py`

- [ ] **Step 1: 失败测试**
```python
# tests/test_auth/test_index_states.py
from src.service.indexing.states import (
    QUEUED, CLONING, BUILDING_GRAPH, CROSS_SERVICE, EMBEDDING, INTERPRETING, DONE, FAILED,
    PHASE_ORDER, is_terminal,
)


def test_phase_order():
    assert PHASE_ORDER == [CLONING, BUILDING_GRAPH, CROSS_SERVICE, EMBEDDING, INTERPRETING]


def test_terminal():
    assert is_terminal(DONE) and is_terminal(FAILED)
    assert not is_terminal(QUEUED) and not is_terminal(CLONING)
```

- [ ] **Step 2: 跑测试确认失败**（ModuleNotFoundError）。

- [ ] **Step 3: 实现**
```python
# src/service/indexing/__init__.py
```
```python
# src/service/indexing/states.py
"""索引作业状态常量 + 顺序。设计 §9 状态机。"""
QUEUED = "queued"
CLONING = "cloning"
BUILDING_GRAPH = "building_graph"
CROSS_SERVICE = "cross_service"
EMBEDDING = "embedding"
INTERPRETING = "interpreting"
DONE = "done"
FAILED = "failed"

# 工作阶段顺序（不含 queued/done/failed）；indexer 按此上报进度
PHASE_ORDER = [CLONING, BUILDING_GRAPH, CROSS_SERVICE, EMBEDDING, INTERPRETING]

_TERMINAL = {DONE, FAILED}


def is_terminal(status: str) -> bool:
    """作业是否已到终态（done/failed）。"""
    return status in _TERMINAL
```

- [ ] **Step 4: 跑测试确认通过**（2 passed）。

- [ ] **Step 5: 提交**
```bash
git add src/service/indexing/__init__.py src/service/indexing/states.py tests/test_auth/test_index_states.py
git commit -m "feat(indexing): 作业状态常量 + 阶段顺序"
```

---

## Task 3: 入队 + 原子认领（queue.py）

**Files:** Create `src/service/indexing/queue.py`；Test `tests/test_auth/test_index_queue.py`

> 认领用 DB 无关的"乐观原子认领"：`UPDATE index_jobs SET status=cloning,worker_id,started_at WHERE id=(选最早 queued) AND status='queued'`，按 rowcount 判是否抢到。生产 MySQL 可叠加 `FOR UPDATE SKIP LOCKED` 提并发（注释标注）；SQLite 测试用本式即可。

- [ ] **Step 1: 失败测试**
```python
# tests/test_auth/test_index_queue.py
import pytest, pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from src.service.db_models_homepage import Base, IndexJob, Project
from src.service.indexing.queue import enqueue_index_job, claim_next_job
from src.service.indexing.states import CLONING, QUEUED


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_enqueue_and_claim(maker):
    async with maker() as s:
        s.add(Project(id="p1", name="P1"))
        await s.commit()
        job = await enqueue_index_job(s, project_id="p1", type_="full_index", trigger="manual")
        assert job.status == QUEUED
        await s.commit()

    async with maker() as s:
        claimed = await claim_next_job(s, worker_id="w1")
        assert claimed is not None
        assert claimed.status == CLONING
        assert claimed.worker_id == "w1"
        assert claimed.started_at is not None

    async with maker() as s:
        # 没有更多 queued → None
        assert await claim_next_job(s, worker_id="w1") is None


@pytest.mark.asyncio
async def test_dedup_key_skips_duplicate(maker):
    async with maker() as s:
        s.add(Project(id="p1", name="P1")); await s.commit()
        j1 = await enqueue_index_job(s, project_id="p1", type_="incremental", trigger="webhook", dedup_key="d1")
        await s.commit()
        j2 = await enqueue_index_job(s, project_id="p1", type_="incremental", trigger="webhook", dedup_key="d1")
        await s.commit()
        assert j2.id == j1.id   # 相同 dedup_key 不重复入队，返回已有
```

- [ ] **Step 2: 跑测试确认失败**。

- [ ] **Step 3: 实现**
```python
# src/service/indexing/queue.py
"""索引作业入队 + 原子认领。设计 §9。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.service.db_models_homepage import IndexJob
from src.service.indexing.states import QUEUED, CLONING


async def enqueue_index_job(
    session: AsyncSession, *, project_id: str, type_: str, trigger: str,
    dedup_key: Optional[str] = None, commit_sha: Optional[str] = None,
) -> IndexJob:
    """入队一条作业。dedup_key 命中未终态的同 key 作业则返回已有（webhook 去重）。"""
    if dedup_key:
        existing = (await session.execute(
            select(IndexJob).where(IndexJob.dedup_key == dedup_key)
            .where(IndexJob.status.notin_(["done", "failed"]))
        )).scalars().first()
        if existing is not None:
            return existing
    job = IndexJob(
        id=f"job-{uuid.uuid4().hex[:16]}", project_id=project_id, type=type_,
        status=QUEUED, trigger=trigger, dedup_key=dedup_key, commit_sha=commit_sha,
    )
    session.add(job)
    await session.flush()
    return job


async def claim_next_job(session: AsyncSession, *, worker_id: str) -> Optional[IndexJob]:
    """原子认领最早的 queued 作业 → 置 cloning + worker_id + started_at；抢不到返 None。

    生产 MySQL 并发下应在选 id 的子查询加 FOR UPDATE SKIP LOCKED；SQLite 测试用乐观 rowcount。
    """
    row = (await session.execute(
        select(IndexJob.id).where(IndexJob.status == QUEUED).order_by(IndexJob.created_at).limit(1)
    )).scalars().first()
    if row is None:
        return None
    now = datetime.now(timezone.utc)
    res = await session.execute(
        update(IndexJob).where(IndexJob.id == row, IndexJob.status == QUEUED)
        .values(status=CLONING, worker_id=worker_id, started_at=now)
    )
    await session.flush()
    if res.rowcount != 1:
        return None  # 被别的 worker 抢先
    return (await session.execute(select(IndexJob).where(IndexJob.id == row))).scalar_one()
```

- [ ] **Step 4: 跑测试确认通过**（2 passed）。

- [ ] **Step 5: 提交**
```bash
git add src/service/indexing/queue.py tests/test_auth/test_index_queue.py
git commit -m "feat(indexing): 作业入队(去重) + 原子认领"
```

---

## Task 4: 作业编排 run_one_job（runner.py）

**Files:** Create `src/service/indexing/runner.py`；Test `tests/test_auth/test_index_runner.py`

> 核心：认领→调可注入 `indexer`（async，收 progress 回调，返回 commit_sha 或抛异常）→进度写库→成功置 done+回填 project→失败置 failed/重试（attempts+1，<MAX 则重排 queued）。用 fake indexer 单测，不碰真实 clone/pipeline。

- [ ] **Step 1: 失败测试**
```python
# tests/test_auth/test_index_runner.py
import pytest, pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select
from src.service.db_models_homepage import Base, IndexJob, Project
from src.service.indexing.queue import enqueue_index_job
from src.service.indexing.runner import run_one_job, MAX_ATTEMPTS
from src.service.indexing.states import DONE, FAILED, QUEUED, BUILDING_GRAPH


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def _seed(maker):
    async with maker() as s:
        s.add(Project(id="p1", name="P1"))
        await enqueue_index_job(s, project_id="p1", type_="full_index", trigger="manual")
        await s.commit()


@pytest.mark.asyncio
async def test_run_one_job_success(maker):
    await _seed(maker)
    phases = []

    async def fake_indexer(job, progress):
        await progress(BUILDING_GRAPH, 50)
        phases.append(BUILDING_GRAPH)
        return "a" * 40

    handled = await run_one_job(maker, worker_id="w1", indexer=fake_indexer)
    assert handled is True
    assert phases == [BUILDING_GRAPH]
    async with maker() as s:
        job = (await s.execute(select(IndexJob))).scalar_one()
        proj = (await s.execute(select(Project))).scalar_one()
        assert job.status == DONE
        assert job.commit_sha == "a" * 40
        assert job.finished_at is not None
        assert proj.status == "ready"
        assert proj.last_synced_commit == "a" * 40


@pytest.mark.asyncio
async def test_run_one_job_failure_requeues(maker):
    await _seed(maker)

    async def boom(job, progress):
        raise RuntimeError("clone failed")

    await run_one_job(maker, worker_id="w1", indexer=boom)
    async with maker() as s:
        job = (await s.execute(select(IndexJob))).scalar_one()
        assert job.attempts == 1
        assert job.status == QUEUED          # <MAX 重排
        assert "clone failed" in (job.error or "")


@pytest.mark.asyncio
async def test_run_one_job_failure_terminal_after_max(maker):
    await _seed(maker)

    async def boom(job, progress):
        raise RuntimeError("x")

    for _ in range(MAX_ATTEMPTS):
        await run_one_job(maker, worker_id="w1", indexer=boom)
    async with maker() as s:
        job = (await s.execute(select(IndexJob))).scalar_one()
        assert job.attempts == MAX_ATTEMPTS
        assert job.status == FAILED          # 达上限置终态


@pytest.mark.asyncio
async def test_run_one_job_noop_when_empty(maker):
    async with maker() as s:
        s.add(Project(id="p1", name="P1")); await s.commit()
    assert await run_one_job(maker, worker_id="w1", indexer=None) is False
```

- [ ] **Step 2: 跑测试确认失败**。

- [ ] **Step 3: 实现**
```python
# src/service/indexing/runner.py
"""索引作业编排：认领一条 → 跑 indexer → 写进度/终态/重试。设计 §9。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.service.db_models_homepage import IndexJob, Project
from src.service.indexing.queue import claim_next_job
from src.service.indexing.states import DONE, FAILED, QUEUED

MAX_ATTEMPTS = 3

# indexer：收 (job, progress_cb)；progress_cb(phase:str, percent:int)；返回 commit_sha 或抛异常
ProgressCb = Callable[[str, int], Awaitable[None]]
IndexerFn = Callable[[IndexJob, ProgressCb], Awaitable[str]]


async def run_one_job(
    maker: async_sessionmaker[AsyncSession], *, worker_id: str, indexer: Optional[IndexerFn],
) -> bool:
    """认领并处理一条作业。返回 True=处理了一条，False=队列空。"""
    async with maker() as s:
        job = await claim_next_job(s, worker_id=worker_id)
        await s.commit()
    if job is None:
        return False

    async def progress(phase: str, percent: int) -> None:
        async with maker() as s2:
            j = (await s2.execute(select(IndexJob).where(IndexJob.id == job.id))).scalar_one()
            j.status = phase
            j.progress = {"phase": phase, "percent": percent}
            p = (await s2.execute(select(Project).where(Project.id == j.project_id))).scalar_one_or_none()
            if p is not None:
                p.status = "indexing"
                p.indexing_progress = {"phase": phase, "percent": percent}
            await s2.commit()

    try:
        commit_sha = await indexer(job, progress)  # type: ignore[misc]
    except Exception as e:  # noqa: BLE001 — 作业失败要兜住、记 error、决定重试
        async with maker() as s3:
            j = (await s3.execute(select(IndexJob).where(IndexJob.id == job.id))).scalar_one()
            j.attempts += 1
            j.error = str(e)[:2000]
            j.status = FAILED if j.attempts >= MAX_ATTEMPTS else QUEUED
            if j.status == FAILED:
                j.finished_at = datetime.now(timezone.utc)
            await s3.commit()
        return True

    async with maker() as s4:
        j = (await s4.execute(select(IndexJob).where(IndexJob.id == job.id))).scalar_one()
        j.status = DONE
        j.commit_sha = commit_sha
        j.finished_at = datetime.now(timezone.utc)
        j.progress = {"phase": DONE, "percent": 100}
        p = (await s4.execute(select(Project).where(Project.id == j.project_id))).scalar_one_or_none()
        if p is not None:
            p.status = "ready"
            p.last_synced_commit = commit_sha
            p.last_synced_at = datetime.now(timezone.utc)
        await s4.commit()
    return True
```

- [ ] **Step 4: 跑测试确认通过**（4 passed）。

- [ ] **Step 5: 提交**
```bash
git add src/service/indexing/runner.py tests/test_auth/test_index_runner.py
git commit -m "feat(indexing): run_one_job 编排（进度/成功回填/失败重试）"
```

---

## Task 5: 默认真实 indexer（real_indexer.py）

**Files:** Create `src/service/indexing/real_indexer.py`；Test `tests/test_auth/test_real_indexer.py`

> 真实 indexer = 用 P1 `GitHubAppProvider.clone` 拉代码到受管目录 → 上报 cloning/building_graph... → shell 出 `src.pipeline.cli` 跑索引。单测只验"命令构造 + provider/clone 被正确调用 + 进度上报顺序"（mock provider + mock subprocess）；真实端到端 gated。

- [ ] **Step 1: 失败测试**
```python
# tests/test_auth/test_real_indexer.py
import os, pytest
from src.service.indexing.real_indexer import build_pipeline_args, make_real_indexer
from src.service.indexing.states import CLONING, BUILDING_GRAPH, INTERPRETING


def test_build_pipeline_args():
    args = build_pipeline_args(repo_dir="/repos/p1", output_dir="/tmp/out")
    assert args[:3] == ["python", "-m", "src.pipeline.cli"]
    assert "/repos/p1" in args
    assert "--with-interpretation" in args
    assert "--output-dir" in args and "/tmp/out" in args


@pytest.mark.asyncio
async def test_make_real_indexer_calls_clone_and_reports(monkeypatch, tmp_path):
    calls = {"cloned": False, "phases": []}

    class FakeProvider:
        async def clone(self, installation_id, full_name, ref, subpath, dest):
            calls["cloned"] = (installation_id, full_name, ref, dest)
            return "b" * 40

    async def fake_run_pipeline(args, cwd=None):
        return ""  # 假装 pipeline 成功

    indexer = make_real_indexer(
        provider=FakeProvider(), installation_id=7, full_name="o/r", ref="master",
        subpath=None, repos_root=str(tmp_path), run_pipeline=fake_run_pipeline,
    )

    class _Job:  # 最小 job 替身
        id = "job-x"; project_id = "p1"
    async def progress(phase, percent):
        calls["phases"].append(phase)

    sha = await indexer(_Job(), progress)
    assert sha == "b" * 40
    assert calls["cloned"][1] == "o/r"
    assert CLONING in calls["phases"]
    assert BUILDING_GRAPH in calls["phases"]
    assert INTERPRETING in calls["phases"]
```

- [ ] **Step 2: 跑测试确认失败**。

- [ ] **Step 3: 实现**
```python
# src/service/indexing/real_indexer.py
"""默认真实 indexer：clone(P1) + shell 出 pipeline。设计 §9。
为可测，clone/pipeline 均可注入；progress 上报覆盖 PHASE_ORDER。"""
from __future__ import annotations

import asyncio
import os
import subprocess
from typing import Awaitable, Callable, Optional

from src.service.indexing.states import (
    CLONING, BUILDING_GRAPH, CROSS_SERVICE, EMBEDDING, INTERPRETING,
)

RunPipelineFn = Callable[..., Awaitable[str]]


def build_pipeline_args(repo_dir: str, output_dir: str) -> list[str]:
    """构造 pipeline CLI 命令（含解读）。"""
    return ["python", "-m", "src.pipeline.cli", repo_dir,
            "--with-interpretation", "--output-dir", output_dir]


async def _default_run_pipeline(args: list[str], cwd: Optional[str] = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"pipeline 失败(rc={proc.returncode}): {err.decode('utf-8','replace')[:2000]}")
    return out.decode("utf-8", "replace")


def make_real_indexer(*, provider, installation_id: int, full_name: str, ref: str,
                      subpath: Optional[str], repos_root: str,
                      run_pipeline: RunPipelineFn = _default_run_pipeline):
    """造一个 IndexerFn：clone → 跑 pipeline，按 PHASE_ORDER 上报进度。"""
    async def _indexer(job, progress) -> str:
        await progress(CLONING, 5)
        dest = os.path.join(repos_root, job.project_id)
        commit_sha = await provider.clone(installation_id, full_name, ref, subpath, dest)
        # pipeline 一次跑完图/跨服务/向量/解读；这里按阶段粗粒度上报（pipeline 内部细分暂不回传）
        await progress(BUILDING_GRAPH, 30)
        out_dir = os.path.join(repos_root, f"{job.project_id}.out")
        await progress(CROSS_SERVICE, 45)
        await progress(EMBEDDING, 60)
        await run_pipeline(build_pipeline_args(dest, out_dir), cwd=None)
        await progress(INTERPRETING, 90)
        return commit_sha
    return _indexer
```

- [ ] **Step 4: 跑测试确认通过**（2 passed）。

- [ ] **Step 5（可选 gated 集成，不阻塞）**：真实跑一次 `KE_GATED_CLONE=1` + 真 pipeline 需 Neo4j/Weaviate，留到服务器联调；本任务不做。

- [ ] **Step 6: 提交**
```bash
git add src/service/indexing/real_indexer.py tests/test_auth/test_real_indexer.py
git commit -m "feat(indexing): 默认真实 indexer（clone + shell pipeline，可注入）"
```

---

## Task 6: worker 循环 + 入口（indexer.py）+ systemd 单元

**Files:** Create `src/service/indexer.py`；Create `deploy/ke-indexer.service`；Test `tests/test_auth/test_indexer_loop.py`

> 循环：反复 `run_one_job`；空闲 sleep；可注入 indexer + 跑几轮即停（测试用）。入口 `python -m src.service.indexer` 用真实 indexer（但真实 indexer 需 provider/installation 上下文——P2 仅提供"按 project 装配真实 indexer"的占位，完整装配在 P3 连接 API 落地后接通；P2 worker 入口先支持 fake/单 project 手动模式 + 留 TODO）。

- [ ] **Step 1: 失败测试**（只测循环控制，不接真实索引）
```python
# tests/test_auth/test_indexer_loop.py
import pytest, pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from src.service.db_models_homepage import Base, Project
from src.service.indexing.queue import enqueue_index_job
from src.service.indexer import run_worker_loop


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_worker_loop_drains_then_stops(maker):
    async with maker() as s:
        s.add(Project(id="p1", name="P1"))
        await enqueue_index_job(s, project_id="p1", type_="full_index", trigger="manual")
        await enqueue_index_job(s, project_id="p1", type_="reindex", trigger="manual")
        await s.commit()

    done = []
    async def fake_indexer(job, progress):
        done.append(job.id)
        return "c" * 40

    # max_rounds 跑完 2 条后停；idle_sleep=0 不真睡
    processed = await run_worker_loop(maker, worker_id="w1", indexer=fake_indexer,
                                      max_rounds=5, idle_sleep=0)
    assert processed == 2
    assert len(done) == 2
```

- [ ] **Step 2: 跑测试确认失败**。

- [ ] **Step 3: 实现**
```python
# src/service/indexer.py
"""ke-indexer worker：循环认领并处理索引作业。
入口 `python -m src.service.indexer`（systemd 拉起）。设计 §9。"""
from __future__ import annotations

import asyncio
import os
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.service.indexing.runner import IndexerFn, run_one_job


async def run_worker_loop(
    maker: async_sessionmaker[AsyncSession], *, worker_id: str, indexer: Optional[IndexerFn],
    max_rounds: Optional[int] = None, idle_sleep: float = 2.0,
) -> int:
    """循环处理作业。max_rounds=None 永久跑（生产）；给数字则跑够轮数即停（测试）。
    返回累计处理的作业数。每轮空队列就 sleep(idle_sleep)。"""
    processed = 0
    rounds = 0
    while max_rounds is None or rounds < max_rounds:
        rounds += 1
        handled = await run_one_job(maker, worker_id=worker_id, indexer=indexer)
        if handled:
            processed += 1
        else:
            if max_rounds is not None:
                break  # 测试：队列空即停
            await asyncio.sleep(idle_sleep)
    return processed


def _main() -> None:  # pragma: no cover — 进程入口
    from src.service.db import get_session_maker
    worker_id = os.getenv("KE_INDEXER_WORKER_ID", "ke-indexer-1")
    # TODO(P3)：真实 indexer 需按 job→project→scm_connection 装配 GitHubAppProvider + installation。
    #          P3 连接 API 落地后在此注入 make_real_indexer；当前入口仅占位，生产启用待 P3。
    raise SystemExit("ke-indexer 入口待 P3 接通真实 indexer 装配（见 TODO）")


if __name__ == "__main__":  # pragma: no cover
    _main()
```

- [ ] **Step 4: 跑测试确认通过**（1 passed）。

- [ ] **Step 5: systemd 单元（运维文档，不测）**
```ini
# deploy/ke-indexer.service
[Unit]
Description=KE indexer worker (clone + 索引作业)
After=network.target mysql.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/knowledge-engineering
EnvironmentFile=/opt/knowledge-engineering/.env
ExecStart=/opt/knowledge-engineering/venv/bin/python -m src.service.indexer
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 6: P2 回归 + 提交**
```bash
./venv/bin/python -m pytest tests/test_auth/test_index_job_model.py tests/test_auth/test_index_states.py tests/test_auth/test_index_queue.py tests/test_auth/test_index_runner.py tests/test_auth/test_real_indexer.py tests/test_auth/test_indexer_loop.py -v
git add src/service/indexer.py deploy/ke-indexer.service tests/test_auth/test_indexer_loop.py
git commit -m "feat(indexing): ke-indexer worker 循环 + 入口 + systemd 单元"
```

---

## 完成标准（P2 Done）
- `index_jobs` 表 + `IndexJob` 模型 + 迁移（单一 head）。
- `src/service/indexing/`：states / queue(入队+去重+原子认领) / runner(编排+重试) / real_indexer(clone+pipeline,可注入)。
- `src/service/indexer.py` worker 循环 + 入口（真实装配留 P3 TODO）+ systemd 单元。
- 全部单测绿；真实端到端索引（需 Neo4j/Weaviate）留服务器联调。
- **不含**：路由/webhook 入队（P3）、真实 indexer 的 per-job provider 装配（P3 接通）。

## 待后续 Plan 衔接
- P3 连接 API：webhook/手动触发 → `enqueue_index_job`；并在 `src/service/indexer.py::_main` 接通"job→project→scm_connection→GitHubAppProvider+installation"装配，启用真实 worker。
- 生产 MySQL：`claim_next_job` 子查询加 `FOR UPDATE SKIP LOCKED` 提并发（注释已标）。
- 进度细化：pipeline 内部阶段回传（当前 real_indexer 粗粒度上报）。
- **卡死作业回收 reaper（P2 终审提出，建议进 P3）**：worker 进程中途死亡时作业卡在非终态（cloning 等）永不重排——`attempts`/`MAX_ATTEMPTS` 只防 indexer 异常、不防 worker 死亡。加一个扫描：把非终态、`started_at` 超过租约超时的作业重排 queued（`started_at` 已在认领时记录，数据齐备）。
- **requeue 清理（minor）**：失败重排回 `QUEUED` 时清掉 `worker_id`/`started_at`（当前残留上次认领值，仅观测性问题，不影响正确性）。
- **常量一致性（minor）**：`queue.py` 去重用字面量 `["done","failed"]`，应改用 `states.DONE/FAILED`。
