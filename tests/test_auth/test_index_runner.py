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
        assert job.status == QUEUED
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
        assert job.status == FAILED


@pytest.mark.asyncio
async def test_run_one_job_noop_when_empty(maker):
    async with maker() as s:
        s.add(Project(id="p1", name="P1")); await s.commit()
    assert await run_one_job(maker, worker_id="w1", indexer=None) is False
