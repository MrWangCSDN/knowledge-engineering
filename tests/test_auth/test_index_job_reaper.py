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
