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


@pytest.mark.asyncio
async def test_unbound_project_raises(maker, tmp_path):
    # 工程未绑定任何 SCM 连接（scm_connection_id 为 NULL）
    async with maker() as s:
        s.add(Project(id="p1", name="P1")); await s.commit()   # 未绑定
    indexer = build_indexer_for_job(maker, provider=_FakeProvider(), repos_root=str(tmp_path))
    class _Job: id="j1"; project_id="p1"
    async def progress(ph, pct): pass
    # 期望抛出包含"未绑定"的 RuntimeError
    with pytest.raises(RuntimeError, match="未绑定"):
        await indexer(_Job(), progress)


@pytest.mark.asyncio
async def test_pat_connection_raises(maker, tmp_path):
    # PAT 类型连接：github_installation_id 为 None，不能走 GitHub App clone 路径
    async with maker() as s:
        s.add(ScmConnection(id="c1", provider="github", auth_type="pat",
                            github_installation_id=None, account_login="o", status="active"))
        s.add(Project(id="p1", name="P1", scm_connection_id="c1", repo_external_id=42,
                      repo_full_name="o/r", ref="master", ref_type="branch"))
        await s.commit()
    indexer = build_indexer_for_job(maker, provider=_FakeProvider(), repos_root=str(tmp_path))
    class _Job: id="j1"; project_id="p1"
    async def progress(ph, pct): pass
    # 期望抛出包含"GitHub App"的 RuntimeError
    with pytest.raises(RuntimeError, match="GitHub App"):
        await indexer(_Job(), progress)


@pytest.mark.asyncio
async def test_missing_connection_raises(maker, tmp_path):
    # 工程绑定了一个不存在的连接 id（模拟连接被删后 FK 未及时置空的窗口）
    async with maker() as s:
        s.add(Project(id="p1", name="P1", scm_connection_id="ghost", repo_external_id=42,
                      repo_full_name="o/r", ref="master", ref_type="branch")); await s.commit()
    indexer = build_indexer_for_job(maker, provider=_FakeProvider(), repos_root=str(tmp_path))
    class _Job: id="j1"; project_id="p1"
    async def progress(ph, pct): pass
    # 期望抛出包含"不存在"的 RuntimeError（Fix A 新增路径）
    with pytest.raises(RuntimeError, match="不存在"):
        await indexer(_Job(), progress)
