"""P5b 同步健康度面板 GET /projects/{id}/sync-health。"""
import pytest
import pytest_asyncio
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
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


def _app(maker, *, require_role=None):
    app = FastAPI()
    async def _get_db():
        async with maker() as s:
            yield s
    app.include_router(create_scm_binding_routes(
        get_current_user=lambda: _User(), get_db=_get_db,
        require_role=require_role or (lambda role: (lambda: None)),
    ))
    return app


_BASE = datetime(2026, 6, 22, 10, 0, 0, tzinfo=timezone.utc)


async def _add_project(maker, *, pid="p1", status="indexing", last_synced_at=None, last_synced_commit=None):
    async with maker() as s:
        s.add(Project(id=pid, name="P", status=status,
                      last_synced_at=last_synced_at, last_synced_commit=last_synced_commit))
        await s.commit()


async def _add_job(maker, *, jid, status, project_id="p1", error=None, created_at=None,
                   finished_at=None, started_at=None, lease_expires=None):
    async with maker() as s:
        s.add(IndexJob(id=jid, project_id=project_id, type="full", status=status, trigger="manual",
                       error=error, created_at=created_at, finished_at=finished_at,
                       started_at=started_at, lease_expires=lease_expires))
        await s.commit()


@pytest.mark.asyncio
async def test_sync_health_404_when_project_missing(maker):
    c = TestClient(_app(maker))
    assert c.get("/projects/nope/sync-health").status_code == 404


@pytest.mark.asyncio
async def test_sync_health_reporter_gate_denied(maker):
    await _add_project(maker)
    def _deny(role):
        def _dep():
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail="no")
        return _dep
    c = TestClient(_app(maker, require_role=_deny))
    assert c.get("/projects/p1/sync-health").status_code == 403


@pytest.mark.asyncio
async def test_sync_health_rich(maker):
    await _add_project(maker, status="indexing",
                       last_synced_at=_BASE - timedelta(hours=24), last_synced_commit="abc123")
    await _add_job(maker, jid="q1", status="queued", created_at=_BASE - timedelta(hours=5))
    await _add_job(maker, jid="q2", status="queued", created_at=_BASE - timedelta(hours=4))
    await _add_job(maker, jid="run1", status="cloning", created_at=_BASE - timedelta(hours=3))
    await _add_job(maker, jid="f1", status="failed", error="boom-old",
                   created_at=_BASE - timedelta(hours=6), finished_at=_BASE - timedelta(hours=6))
    await _add_job(maker, jid="f2", status="failed", error="boom-mid",
                   created_at=_BASE - timedelta(hours=2), finished_at=_BASE - timedelta(hours=2))
    await _add_job(maker, jid="f3", status="failed", error="boom-new",
                   created_at=_BASE - timedelta(hours=1), finished_at=_BASE - timedelta(minutes=30))
    c = TestClient(_app(maker))
    body = c.get("/projects/p1/sync-health").json()
    assert body["status"] == "indexing"
    assert body["last_synced_commit"] == "abc123"
    assert body["job_counts"] == {"queued": 2, "running": 1, "failed": 3}
    assert body["latest_job"]["job_id"] == "f3"
    assert body["last_error"] == "boom-new"
    assert body["staleness_hours"] is not None and body["staleness_hours"] >= 23


@pytest.mark.asyncio
async def test_sync_health_never_synced_and_no_jobs(maker):
    await _add_project(maker, status="configured", last_synced_at=None)
    c = TestClient(_app(maker))
    body = c.get("/projects/p1/sync-health").json()
    assert body["staleness_hours"] is None
    assert body["job_counts"] == {"queued": 0, "running": 0, "failed": 0}
    assert body["latest_job"] is None and body["last_error"] is None
    assert body["is_stuck"] is False


@pytest.mark.asyncio
async def test_sync_health_is_stuck_true_when_lease_expired(maker):
    await _add_project(maker)
    now = datetime.now(timezone.utc)
    await _add_job(maker, jid="stuck", status="embedding", lease_expires=now - timedelta(seconds=10))
    c = TestClient(_app(maker))
    assert c.get("/projects/p1/sync-health").json()["is_stuck"] is True


@pytest.mark.asyncio
async def test_sync_health_is_stuck_false_when_lease_future(maker):
    await _add_project(maker)
    now = datetime.now(timezone.utc)
    await _add_job(maker, jid="ok", status="embedding", lease_expires=now + timedelta(hours=1))
    c = TestClient(_app(maker))
    assert c.get("/projects/p1/sync-health").json()["is_stuck"] is False


@pytest.mark.asyncio
async def test_sync_health_is_stuck_null_lease_started_fallback(maker):
    await _add_project(maker)
    now = datetime.now(timezone.utc)
    await _add_job(maker, jid="old", status="cloning", lease_expires=None,
                   started_at=now - timedelta(seconds=7200))
    c = TestClient(_app(maker))
    assert c.get("/projects/p1/sync-health").json()["is_stuck"] is True
