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
