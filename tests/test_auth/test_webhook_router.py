import hashlib, hmac, json
import pytest, pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select
from src.service.db_models_homepage import Base, Project, IndexJob
from src.service.webhook_router import create_webhook_routes


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


def _app(maker, secret="whsec"):
    app = FastAPI()
    async def _get_db():
        async with maker() as s:
            yield s
    app.include_router(create_webhook_routes(get_db=_get_db, webhook_secret=secret))
    return app


def _sig(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.mark.asyncio
async def test_webhook_enqueues_for_bound_branch(maker):
    async with maker() as s:
        s.add(Project(id="p1", name="P1", scm_connection_id="c1", repo_external_id=42,
                      repo_full_name="o/r", ref="master", ref_type="branch")); await s.commit()
    c = TestClient(_app(maker))
    body = json.dumps({"ref": "refs/heads/master", "after": "a"*40, "repository": {"id": 42}}).encode()
    r = c.post("/webhooks/github", content=body,
               headers={"X-Hub-Signature-256": _sig("whsec", body), "X-GitHub-Event": "push",
                        "X-GitHub-Delivery": "d1"})
    assert r.status_code == 200
    async with maker() as s:
        jobs = (await s.execute(select(IndexJob).where(IndexJob.project_id == "p1"))).scalars().all()
        assert len(jobs) == 1 and jobs[0].type == "incremental" and jobs[0].dedup_key == "d1"


def test_webhook_bad_signature_401(maker):
    c = TestClient(_app(maker))
    body = b'{"ref":"refs/heads/master"}'
    r = c.post("/webhooks/github", content=body,
               headers={"X-Hub-Signature-256": "sha256=bad", "X-GitHub-Event": "push", "X-GitHub-Delivery": "d2"})
    assert r.status_code == 401
