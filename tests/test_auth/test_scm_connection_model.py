"""scm_connection 账号级连接模型测试（in-memory SQLite）。"""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select

from src.service.db_models_homepage import Base, ScmConnection


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s


@pytest.mark.asyncio
async def test_scm_connection_roundtrip(session):
    conn = ScmConnection(
        id="conn-1",
        provider="github",
        auth_type="github_app",
        github_installation_id=12345,
        account_login="macrozheng",
        status="active",
        created_by="alice",
    )
    session.add(conn)
    await session.commit()

    row = (await session.execute(select(ScmConnection).where(ScmConnection.id == "conn-1"))).scalar_one()
    assert row.provider == "github"
    assert row.auth_type == "github_app"
    assert row.github_installation_id == 12345
    assert row.account_login == "macrozheng"
    assert row.status == "active"
    assert row.gitlab_instance_url is None
