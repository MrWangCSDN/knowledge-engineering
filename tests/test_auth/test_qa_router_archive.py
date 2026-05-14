"""验证 qa_router 归档相关行为：list 过滤 / archive / unarchive / explain 409。"""
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.service import auth_security as sec
from src.service.auth_models import User
from src.service.auth_router import router as auth_router
from src.service.db import Base, get_db
from src.service.db_models_homepage import (
    Project as ProjectModel,
    QASession,
    UserProjectAccess,
)
from src.service.project_router import router as project_router
from src.service.qa_router import router as qa_router


# ───────── 共享 fixture ─────────

@pytest_asyncio.fixture
async def session_maker(monkeypatch):
    monkeypatch.setenv("KE_JWT_SECRET", "x" * 32)
    monkeypatch.setenv("KE_COOKIE_SECURE", "false")
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SM = async_sessionmaker(eng, expire_on_commit=False)
    async with SM() as s:
        # alice 是 project p1 的 reporter
        s.add(User(id=1, email="alice@x.com", username="alice",
                   hashed_password=sec.hash_password("12345678"),
                   is_active=True, is_admin=False))
        s.add(ProjectModel(id="p1", name="P1", status="ready"))
        s.add(UserProjectAccess(user_id=1, project_id="p1", role="reporter"))
        await s.commit()
    return SM


def _build_app(session_maker):
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(project_router)
    app.include_router(qa_router)

    async def override_db():
        async with session_maker() as s:
            yield s
            await s.commit()
    app.dependency_overrides[get_db] = override_db
    return app


def _login(client, username="alice", password="12345678"):
    """登录并返回 access_token。"""
    resp = client.post("/auth/login",
                       json={"username": username, "password": password,
                             "remember_me": False})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


# ───────── Task 3 测试 ─────────

@pytest.mark.asyncio
async def test_list_sessions_filters_out_archived(session_maker):
    """list /qa/sessions 默认只返回 archived_at IS NULL 的 session。"""
    # 准备数据：插 2 个 session，1 活动 1 归档
    async with session_maker() as s:
        s.add(QASession(id="sess_active", project_id="p1", user_id=1,
                        title="活动 session", message_count=2))
        s.add(QASession(id="sess_archived", project_id="p1", user_id=1,
                        title="已归档 session", message_count=3,
                        archived_at=datetime(2026, 5, 10, 0, 0, 0)))
        await s.commit()

    app = _build_app(session_maker)
    client = TestClient(app)
    token = _login(client)

    resp = client.get("/projects/p1/qa/sessions",
                      headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    data = resp.json()
    ids = [s["id"] for s in data["sessions"]]
    # 只看到活动，看不到归档
    assert "sess_active" in ids
    assert "sess_archived" not in ids


# ───────── Task 4 测试 ─────────


@pytest.mark.asyncio
async def test_archive_session_success(session_maker):
    """归档自己的 session → 200 + archived_at 被设置。"""
    async with session_maker() as s:
        s.add(QASession(id="sess_a", project_id="p1", user_id=1,
                        title="x", message_count=1))
        await s.commit()

    app = _build_app(session_maker)
    client = TestClient(app)
    token = _login(client)

    resp = client.post("/projects/p1/qa/sessions/sess_a/archive",
                       headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "sess_a"
    assert body["archived_at"] is not None  # ISO 8601 字符串

    # DB 状态确认
    async with session_maker() as s:
        sess = await s.get(QASession, "sess_a")
        assert sess.archived_at is not None


@pytest.mark.asyncio
async def test_archive_idempotent(session_maker):
    """归档已归档的 session → 200 + 返回原有 archived_at（不更新时间戳）。"""
    original = datetime(2026, 5, 1, 12, 0, 0)
    async with session_maker() as s:
        s.add(QASession(id="sess_a", project_id="p1", user_id=1,
                        title="x", message_count=1,
                        archived_at=original))
        await s.commit()

    app = _build_app(session_maker)
    client = TestClient(app)
    token = _login(client)

    resp = client.post("/projects/p1/qa/sessions/sess_a/archive",
                       headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    # 时间戳保持不变（幂等性）
    assert body["archived_at"].startswith("2026-05-01")


@pytest.mark.asyncio
async def test_archive_not_owner_returns_404(session_maker):
    """非 owner 归档别人的 session → 404（与现有 delete 一致，不暴露存在性）。"""
    async with session_maker() as s:
        # bob 是另一个用户，session 属于 bob 不属于 alice
        s.add(User(id=2, email="bob@x.com", username="bob",
                   hashed_password=sec.hash_password("12345678"),
                   is_active=True, is_admin=False))
        s.add(QASession(id="sess_b", project_id="p1", user_id=2,
                        title="bob 的 session"))
        await s.commit()

    app = _build_app(session_maker)
    client = TestClient(app)
    token = _login(client)  # alice 登录

    resp = client.post("/api/projects/p1/qa/sessions/sess_b/archive",
                       headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_archive_session_not_found_returns_404(session_maker):
    """session 不存在 → 404。"""
    app = _build_app(session_maker)
    client = TestClient(app)
    token = _login(client)

    resp = client.post("/api/projects/p1/qa/sessions/nonexistent/archive",
                       headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404
