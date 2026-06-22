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

    processed = await run_worker_loop(maker, worker_id="w1", indexer=fake_indexer,
                                      max_rounds=5, idle_sleep=0)
    assert processed == 2
    assert len(done) == 2
