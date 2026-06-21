import pytest, pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select
from src.service.db_models_homepage import Base, Project, IndexJob
from src.service.scm_binding_router import create_scm_binding_routes


class _User:
    username = "alice"; is_admin = True


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


def _app(maker):
    app = FastAPI()
    async def _get_db():
        async with maker() as s:
            yield s
    # 单测聚焦绑定逻辑本身，RBAC 用 no-op 注入旁路；真实 RBAC 见 test_scm_binding_rbac.py
    app.include_router(create_scm_binding_routes(
        get_current_user=lambda: _User(), get_db=_get_db,
        require_role=lambda role: (lambda: None),  # no-op：任何 role 都直接放行
    ))
    return app


@pytest.mark.asyncio
async def test_bind_writes_binding_and_enqueues(maker):
    async with maker() as s:
        s.add(Project(id="p1", name="P1")); await s.commit()
    c = TestClient(_app(maker))
    r = c.post("/projects/p1/bind", json={
        "connection_id": "c1", "repo_external_id": 42, "repo_full_name": "o/r",
        "ref": "master", "ref_type": "branch", "subpath": None,
    })
    assert r.status_code == 200
    assert r.json()["job_id"]
    async with maker() as s:
        p = (await s.execute(select(Project).where(Project.id == "p1"))).scalar_one()
        assert p.scm_connection_id == "c1" and p.repo_external_id == 42 and p.ref == "master"
        jobs = (await s.execute(select(IndexJob).where(IndexJob.project_id == "p1"))).scalars().all()
        assert len(jobs) == 1 and jobs[0].type == "full_index"


@pytest.mark.asyncio
async def test_reindex_enqueues(maker):
    async with maker() as s:
        s.add(Project(id="p1", name="P1", scm_connection_id="c1", repo_external_id=42,
                      repo_full_name="o/r", ref="master", ref_type="branch")); await s.commit()
    c = TestClient(_app(maker))
    assert c.post("/projects/p1/reindex").status_code == 200
    async with maker() as s:
        jobs = (await s.execute(select(IndexJob).where(IndexJob.project_id == "p1"))).scalars().all()
        assert len(jobs) == 1 and jobs[0].type == "reindex"


@pytest.mark.asyncio
async def test_index_status(maker):
    async with maker() as s:
        s.add(Project(id="p1", name="P1")); await s.commit()
    c = TestClient(_app(maker))
    c.post("/projects/p1/bind", json={"connection_id": "c1", "repo_external_id": 1,
           "repo_full_name": "o/r", "ref": "master", "ref_type": "branch", "subpath": None})
    st = c.get("/projects/p1/index-status").json()
    assert st["status"] in ("queued", "cloning")


@pytest.mark.asyncio
async def test_reindex_unbound_returns_422(maker):
    async with maker() as s:
        s.add(Project(id="p1", name="P1")); await s.commit()   # 未绑定
    c = TestClient(_app(maker))
    r = c.post("/projects/p1/reindex")
    assert r.status_code == 422
    async with maker() as s:
        jobs = (await s.execute(select(IndexJob).where(IndexJob.project_id == "p1"))).scalars().all()
        assert jobs == []   # 不入队


def test_reindex_missing_project_404(maker):
    c = TestClient(_app(maker))
    assert c.post("/projects/nope/reindex").status_code == 404


def test_index_status_no_jobs_404(maker):
    c = TestClient(_app(maker))
    assert c.get("/projects/nope/index-status").status_code == 404
