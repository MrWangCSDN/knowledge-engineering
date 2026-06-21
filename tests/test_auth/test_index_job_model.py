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
    assert row.attempts == 0
    assert row.progress is None
