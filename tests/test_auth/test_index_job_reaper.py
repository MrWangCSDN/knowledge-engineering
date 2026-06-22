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
    await _add_job(maker, jid="fresh", status="cloning", lease_delta=+3600)
    await _add_job(maker, jid="qd", status="queued", lease_delta=-10)
    await _add_job(maker, jid="dn", status="done", lease_delta=-10)
    await _add_job(maker, jid="fl", status="failed", lease_delta=-10)
    async with maker() as s:
        n = await reclaim_expired_jobs(s, lease_seconds=3600)
        await s.commit()
    assert n == 0
    assert (await _get(maker, "fresh")).status == "cloning"
    assert (await _get(maker, "qd")).status == "queued"


@pytest.mark.asyncio
async def test_reclaim_null_lease_uses_started_at(maker):
    await _add_project(maker)
    await _add_job(maker, jid="old", status="cloning", lease_delta=None, started_delta=-7200)
    await _add_job(maker, jid="new", status="cloning", lease_delta=None, started_delta=-10)
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
    assert lease > now + timedelta(seconds=1500)
