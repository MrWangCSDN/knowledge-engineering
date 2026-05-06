"""验证 /api/projects/{pid}/qa/sessions 系列路由。

  GET    /sessions                 列出当前用户在该工程下的会话（按 updated_at 倒序）
  GET    /sessions/{sid}           会话详情 + 全部消息
  DELETE /sessions/{sid}           删除会话（级联删消息）
  POST   /sessions/{sid}/messages/{mid}/feedback  写反馈（覆盖式 upsert）
"""
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
    QAFeedback,
    QAMessage,
    QASession,
)
from src.service.project_router import router as project_router
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
        s.add(User(email="bob@x.com", username="bob",
                   hashed_password=sec.hash_password("12345678"),
                   is_active=True, is_admin=False))
        s.add(ProjectModel(id="deposit", name="存款系统", status="ready"))
        await s.commit()
    return SM


@pytest.fixture
def client(session_maker):
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(project_router)
    app.include_router(qa_router)

    async def override_db():
        async with session_maker() as s:
            yield s
            await s.commit()
    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


def _login(client: TestClient, username: str = "alice") -> tuple[str, int]:
    r = client.post("/auth/login", json={
        "username": username, "password": "12345678", "remember_me": False
    })
    assert r.status_code == 200
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"})
    return r.json()["access_token"], me.json()["id"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def seeded_session(session_maker):
    """seed: 1 个 alice 的 session，含 user + assistant 2 条消息。"""
    async with session_maker() as s:
        # 拿到 alice 的 id
        result = await s.execute(select(User).where(User.username == "alice"))
        alice = result.scalar_one()
        sess = QASession(
            id="sess_alice_1",
            project_id="deposit",
            user_id=alice.id,
            title="存款开户的设计逻辑",
            message_count=2,
        )
        s.add(sess)
        await s.flush()
        s.add(QAMessage(
            id="msg_u_1", session_id="sess_alice_1",
            role="user", content="存款开户的设计逻辑？",
        ))
        s.add(QAMessage(
            id="msg_a_1", session_id="sess_alice_1",
            role="assistant", content=None,
            sections=[{"type": "overview", "title": "概述", "content": "...", "references": []}],
            msg_metadata={"token_usage": 100, "latency_ms": 1500},
        ))
        await s.commit()
    return "sess_alice_1"


# ───────── GET /sessions ─────────

def test_list_sessions_empty(client):
    token, _ = _login(client)
    r = client.get("/api/projects/deposit/qa/sessions", headers=_auth(token))
    assert r.status_code == 200
    assert r.json() == {"sessions": []}


def test_list_sessions_with_data(client, seeded_session):
    token, _ = _login(client)
    r = client.get("/api/projects/deposit/qa/sessions", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert len(body["sessions"]) == 1
    assert body["sessions"][0]["id"] == seeded_session
    assert body["sessions"][0]["title"] == "存款开户的设计逻辑"
    assert body["sessions"][0]["message_count"] == 2


def test_list_sessions_filters_by_user(client, seeded_session):
    """bob 看不到 alice 的会话。"""
    token, _ = _login(client, username="bob")
    r = client.get("/api/projects/deposit/qa/sessions", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["sessions"] == []


def test_list_sessions_requires_auth(client):
    r = client.get("/api/projects/deposit/qa/sessions")
    assert r.status_code == 401


# ───────── GET /sessions/{sid} ─────────

def test_get_session_with_messages(client, seeded_session):
    token, _ = _login(client)
    r = client.get(
        f"/api/projects/deposit/qa/sessions/{seeded_session}",
        headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["session"]["id"] == seeded_session
    assert len(body["messages"]) == 2
    # 消息按 created_at 顺序
    assert body["messages"][0]["role"] == "user"
    assert body["messages"][1]["role"] == "assistant"
    assert body["messages"][1]["sections"][0]["type"] == "overview"


def test_get_session_404(client):
    token, _ = _login(client)
    r = client.get("/api/projects/deposit/qa/sessions/no-such", headers=_auth(token))
    assert r.status_code == 404


def test_get_session_other_user_forbidden(client, seeded_session):
    """bob 拿不到 alice 的会话。"""
    token, _ = _login(client, username="bob")
    r = client.get(
        f"/api/projects/deposit/qa/sessions/{seeded_session}",
        headers=_auth(token),
    )
    # 设计上是 404（不暴露存在性），不是 403
    assert r.status_code == 404


# ───────── DELETE /sessions/{sid} ─────────

def test_delete_session_cascades_messages(client, seeded_session, session_maker):
    """删 session 应该级联删 message + feedback。"""
    token, _ = _login(client)
    r = client.delete(
        f"/api/projects/deposit/qa/sessions/{seeded_session}",
        headers=_auth(token),
    )
    assert r.status_code == 204

    # 验证 DB 状态：session 没了，message 也没了
    import asyncio
    async def _check():
        async with session_maker() as db:
            sessions = (await db.execute(select(QASession))).scalars().all()
            messages = (await db.execute(select(QAMessage))).scalars().all()
            assert len(sessions) == 0
            assert len(messages) == 0
    asyncio.run(_check())


def test_delete_session_404(client):
    token, _ = _login(client)
    r = client.delete("/api/projects/deposit/qa/sessions/no-such", headers=_auth(token))
    assert r.status_code == 404


def test_delete_other_user_forbidden(client, seeded_session):
    """bob 不能删 alice 的会话。"""
    token, _ = _login(client, username="bob")
    r = client.delete(
        f"/api/projects/deposit/qa/sessions/{seeded_session}",
        headers=_auth(token),
    )
    assert r.status_code == 404


# ───────── POST /sessions/{sid}/messages/{mid}/feedback ─────────

def test_post_feedback_success(client, seeded_session, session_maker):
    token, _ = _login(client)
    r = client.post(
        f"/api/projects/deposit/qa/sessions/{seeded_session}/messages/msg_a_1/feedback",
        headers=_auth(token),
        json={"vote": "up", "comment": "答得不错"},
    )
    assert r.status_code == 204

    import asyncio
    async def _check():
        async with session_maker() as db:
            fb = (await db.execute(select(QAFeedback))).scalars().all()
            assert len(fb) == 1
            assert fb[0].vote == "up"
            assert fb[0].comment == "答得不错"
    asyncio.run(_check())


def test_post_feedback_overwrites_existing(client, seeded_session, session_maker):
    """对同一 message 二次 feedback 应该覆盖（不是创建第二条）。"""
    token, _ = _login(client)
    url = f"/api/projects/deposit/qa/sessions/{seeded_session}/messages/msg_a_1/feedback"
    client.post(url, headers=_auth(token), json={"vote": "up"})
    r2 = client.post(url, headers=_auth(token), json={"vote": "down", "comment": "反悔了"})
    assert r2.status_code == 204

    import asyncio
    async def _check():
        async with session_maker() as db:
            fb = (await db.execute(select(QAFeedback))).scalars().all()
            assert len(fb) == 1
            assert fb[0].vote == "down"
            assert fb[0].comment == "反悔了"
    asyncio.run(_check())


def test_post_feedback_404_on_unknown_message(client, seeded_session):
    token, _ = _login(client)
    r = client.post(
        f"/api/projects/deposit/qa/sessions/{seeded_session}/messages/unknown/feedback",
        headers=_auth(token),
        json={"vote": "up"},
    )
    assert r.status_code == 404


def test_post_feedback_validates_vote(client, seeded_session):
    """vote 只能是 up / down。"""
    token, _ = _login(client)
    r = client.post(
        f"/api/projects/deposit/qa/sessions/{seeded_session}/messages/msg_a_1/feedback",
        headers=_auth(token),
        json={"vote": "maybe"},  # 非法
    )
    assert r.status_code == 422
