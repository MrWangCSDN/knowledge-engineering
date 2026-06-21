import pytest, pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from src.service.db_models_homepage import Base, Project, ScmConnection
from src.service.indexing.assembly import build_indexer_for_job


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


class _FakeProvider:
    async def clone(self, installation_id, full_name, ref, subpath, dest):
        assert installation_id == 7 and full_name == "o/r" and ref == "master"
        return "e" * 40


@pytest.mark.asyncio
async def test_build_indexer_resolves_binding_and_clones(maker, tmp_path):
    async with maker() as s:
        s.add(ScmConnection(id="c1", provider="github", auth_type="github_app",
                            github_installation_id=7, account_login="o", status="active"))
        s.add(Project(id="p1", name="P1", scm_connection_id="c1", repo_external_id=42,
                      repo_full_name="o/r", ref="master", ref_type="branch"))
        await s.commit()

    async def fake_run_pipeline(args, cwd=None):
        return ""

    indexer = build_indexer_for_job(maker, provider=_FakeProvider(), repos_root=str(tmp_path),
                                    run_pipeline=fake_run_pipeline)
    class _Job: id="j1"; project_id="p1"
    phases = []
    async def progress(ph, pct): phases.append(ph)
    sha = await indexer(_Job(), progress)
    assert sha == "e" * 40
    assert "cloning" in phases
