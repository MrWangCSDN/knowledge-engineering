"""验证 /api/projects/{pid}/qa/explain SSE 路由。

mock 掉 retriever / synthesizer，关注路由层逻辑：
  - 工程不存在 → 404
  - 工程 indexing → 409
  - 正常请求 → SSE 流，事件序列正确
  - 持久化：user 消息 + assistant 消息都进了 qa_messages 表
"""
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
    QAMessage,
    QASession,
)
from src.service.project_router import router as project_router
from src.service.qa_engine.retriever import RetrievedContext
from src.service.qa_engine.synthesizer import SynthesizedAnswer
from src.service.qa_router import router as qa_router


# ───────── fixtures ─────────

@pytest_asyncio.fixture
async def session_maker(monkeypatch):
    monkeypatch.setenv("KE_JWT_SECRET", "x" * 32)
    monkeypatch.setenv("KE_COOKIE_SECURE", "false")
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SM = async_sessionmaker(eng, expire_on_commit=False)
    async with SM() as s:
        s.add(User(email="alice@x.com", username="alice",
                   hashed_password=sec.hash_password("12345678"),
                   is_active=True, is_admin=True))
        await s.commit()
    return SM


def _build_app(session_maker, *, retriever=None, synthesizer=None):
    """构造一个新 FastAPI app，注入 mock retriever/synthesizer 到 app.state。"""
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(project_router)
    app.include_router(qa_router)

    async def override_db():
        async with session_maker() as s:
            yield s
            await s.commit()
    app.dependency_overrides[get_db] = override_db

    # 默认 mock：
    if retriever is None:
        retriever = MagicMock()
        retriever.retrieve = AsyncMock(return_value=RetrievedContext(
            question="x", project_id="p",
            entry_candidates=[{"entity_id": "method://m1"}],
        ))
    if synthesizer is None:
        synthesizer = MagicMock()
        synthesizer.synthesize = AsyncMock(return_value=SynthesizedAnswer(
            sections=[{"type": "overview", "title": "概述", "content": "答案", "references": []}],
            token_usage=100,
            cost_yuan=0.05,
        ))
    app.state.qa_retriever = retriever
    app.state.qa_synthesizer = synthesizer
    return app


@pytest.fixture
def client(session_maker):
    app = _build_app(session_maker)
    return TestClient(app)


def _login(client: TestClient) -> str:
    r = client.post("/auth/login", json={
        "username": "alice", "password": "12345678", "remember_me": False
    })
    assert r.status_code == 200
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def seed_ready_project(session_maker):
    async with session_maker() as s:
        s.add(ProjectModel(id="deposit", name="存款系统", status="ready"))
        await s.commit()
    return "deposit"


@pytest_asyncio.fixture
async def seed_indexing_project(session_maker):
    async with session_maker() as s:
        s.add(ProjectModel(id="loan", name="贷款系统", status="indexing"))
        await s.commit()
    return "loan"


# ───────── 错误路径 ─────────

def test_explain_404_project_not_found(client):
    token = _login(client)
    r = client.post(
        "/api/projects/nonexistent/qa/explain",
        headers=_auth(token),
        json={"question": "x"},
    )
    assert r.status_code == 404


def test_explain_409_project_indexing(client, seed_indexing_project):
    token = _login(client)
    r = client.post(
        f"/api/projects/{seed_indexing_project}/qa/explain",
        headers=_auth(token),
        json={"question": "x"},
    )
    assert r.status_code == 409


def test_explain_requires_auth(client, seed_ready_project):
    r = client.post(
        f"/api/projects/{seed_ready_project}/qa/explain",
        json={"question": "x"},
    )
    assert r.status_code == 401


def test_explain_validates_question_not_empty(client, seed_ready_project):
    token = _login(client)
    r = client.post(
        f"/api/projects/{seed_ready_project}/qa/explain",
        headers=_auth(token),
        json={"question": ""},
    )
    assert r.status_code == 422


# ───────── 正常 SSE 流 ─────────

def test_explain_returns_sse_content_type(client, seed_ready_project):
    token = _login(client)
    r = client.post(
        f"/api/projects/{seed_ready_project}/qa/explain",
        headers=_auth(token),
        json={"question": "存款开户"},
    )
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]


def test_explain_sse_events_in_order(client, seed_ready_project):
    token = _login(client)
    with client.stream(
        "POST",
        f"/api/projects/{seed_ready_project}/qa/explain",
        headers=_auth(token),
        json={"question": "存款开户"},
    ) as r:
        body = "".join(r.iter_text())
    # 顺序：meta 必须最早，done 必须最晚
    meta_idx = body.index("event: meta")
    done_idx = body.index("event: done")
    assert meta_idx < done_idx
    assert "event: section_start" in body
    assert "event: content" in body
    assert "event: section_done" in body


# ───────── 持久化 ─────────

@pytest.mark.asyncio
async def test_explain_persists_user_and_assistant_messages(session_maker, seed_ready_project):
    """问完一次后 qa_sessions + qa_messages 表应该有 1 个 session 和 2 条消息。"""
    app = _build_app(session_maker)
    client = TestClient(app)
    token = _login(client)

    with client.stream(
        "POST",
        f"/api/projects/{seed_ready_project}/qa/explain",
        headers=_auth(token),
        json={"question": "存款开户的设计逻辑"},
    ) as r:
        body = "".join(r.iter_text())  # 消费完整流，触发持久化
    assert "event: done" in body

    # 验证 DB 状态
    async with session_maker() as db:
        sess_count = (await db.execute(select(QASession))).scalars().all()
        msg_count = (await db.execute(select(QAMessage))).scalars().all()
        assert len(sess_count) == 1
        assert len(msg_count) == 2  # user + assistant
        assert sess_count[0].project_id == seed_ready_project
        # 标题取问题前 30 字
        assert "存款开户" in (sess_count[0].title or "")
