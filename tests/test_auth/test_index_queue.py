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
        await s.commit()
        assert claimed is not None
        assert claimed.status == CLONING
        assert claimed.worker_id == "w1"
        assert claimed.started_at is not None
    async with maker() as s:
        assert await claim_next_job(s, worker_id="w1") is None


@pytest.mark.asyncio
async def test_dedup_key_skips_duplicate(maker):
    async with maker() as s:
        s.add(Project(id="p1", name="P1")); await s.commit()
        j1 = await enqueue_index_job(s, project_id="p1", type_="incremental", trigger="webhook", dedup_key="d1")
        await s.commit()
        j2 = await enqueue_index_job(s, project_id="p1", type_="incremental", trigger="webhook", dedup_key="d1")
        await s.commit()
        assert j2.id == j1.id
