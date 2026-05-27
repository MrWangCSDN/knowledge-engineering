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
from src.service.qa_engine.retriever import RetrievedContext
from src.service.qa_engine.router import SkillRouter
from src.service.qa_engine.synthesizer import SynthesizedAnswer
from src.service.qa_router import router as qa_router
from src.service.deps_infra import require_infra_healthy  # Task 7：infra 健康检查 dep（测试里 no-op 覆盖）


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
    # 测试里跳过 infra 健康检查（infra 不可用不是这批测试的验证目标）
    app.dependency_overrides[require_infra_healthy] = lambda: None

    # 默认 mock：
    if retriever is None:
        retriever = MagicMock()
        retriever.retrieve = AsyncMock(return_value=RetrievedContext(
            question="x", project_id="p",
            entry_candidates=[{"entity_id": "method://m1"}],
        ))
    if synthesizer is None:
        # spec=['synthesize']：限制 mock 只有这一个属性；防止 sse_emitter 误判"支持流式"
        synthesizer = MagicMock(spec=['synthesize'])
        synthesizer.synthesize = AsyncMock(return_value=SynthesizedAnswer(
            sections=[{"type": "overview", "title": "概述", "content": "答案", "references": []}],
            token_usage=100,
            cost_yuan=0.05,
        ))
    app.state.qa_retriever = retriever
    app.state.qa_synthesizer = synthesizer
    # 真实 SkillRouter（关键词路径无依赖，注入零成本）
    # 不 mock，因为 router 自己是个纯函数，比 mock 更接近真实行为
    app.state.qa_router = SkillRouter()
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

    resp = client.post("/projects/p1/qa/sessions/sess_b/archive",
                       headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_archive_session_not_found_returns_404(session_maker):
    """session 不存在 → 404。"""
    app = _build_app(session_maker)
    client = TestClient(app)
    token = _login(client)

    resp = client.post("/projects/p1/qa/sessions/nonexistent/archive",
                       headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404


# ───────── Task 5 测试 ─────────


@pytest.mark.asyncio
async def test_unarchive_session_success(session_maker):
    """unarchive 已归档 session → 200 + archived_at 清空。"""
    async with session_maker() as s:
        s.add(QASession(id="sess_a", project_id="p1", user_id=1,
                        title="x", message_count=1,
                        archived_at=datetime(2026, 5, 1, 0, 0, 0)))
        await s.commit()

    app = _build_app(session_maker)
    client = TestClient(app)
    token = _login(client)

    resp = client.post("/projects/p1/qa/sessions/sess_a/unarchive",
                       headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "sess_a"
    assert body["archived_at"] is None

    async with session_maker() as s:
        sess = await s.get(QASession, "sess_a")
        assert sess.archived_at is None


@pytest.mark.asyncio
async def test_unarchive_idempotent_on_active_session(session_maker):
    """unarchive 活动 session → 200 + 仍是 null（无操作）。"""
    async with session_maker() as s:
        s.add(QASession(id="sess_a", project_id="p1", user_id=1,
                        title="x", message_count=1))
        await s.commit()

    app = _build_app(session_maker)
    client = TestClient(app)
    token = _login(client)

    resp = client.post("/projects/p1/qa/sessions/sess_a/unarchive",
                       headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["archived_at"] is None


@pytest.mark.asyncio
async def test_unarchive_not_owner_returns_404(session_maker):
    """非 owner unarchive → 404。"""
    async with session_maker() as s:
        s.add(User(id=2, email="bob@x.com", username="bob",
                   hashed_password=sec.hash_password("12345678"),
                   is_active=True, is_admin=False))
        s.add(QASession(id="sess_b", project_id="p1", user_id=2,
                        title="bob",
                        archived_at=datetime(2026, 5, 1, 0, 0, 0)))
        await s.commit()

    app = _build_app(session_maker)
    client = TestClient(app)
    token = _login(client)

    resp = client.post("/projects/p1/qa/sessions/sess_b/unarchive",
                       headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404


# ─── Task 6: GET /api/user/archived-sessions 测试 ─────────


@pytest.mark.asyncio
async def test_list_archived_sessions_cross_project(session_maker):
    """跨工程汇总当前用户的所有归档 session，按工程分组。"""
    async with session_maker() as s:
        # 再加一个 project p2，alice 也是 reporter
        s.add(ProjectModel(id="p2", name="P2", status="ready"))
        s.add(UserProjectAccess(user_id=1, project_id="p2", role="reporter"))
        # alice 在 p1 一个归档 + p1 一个活动 + p2 一个归档
        s.add(QASession(id="sess_a1_arch", project_id="p1", user_id=1,
                        title="p1 归档", message_count=1,
                        archived_at=datetime(2026, 5, 10, 0, 0, 0)))
        s.add(QASession(id="sess_a2_active", project_id="p1", user_id=1,
                        title="p1 活动", message_count=1))
        s.add(QASession(id="sess_a3_arch", project_id="p2", user_id=1,
                        title="p2 归档", message_count=2,
                        archived_at=datetime(2026, 5, 12, 0, 0, 0)))
        await s.commit()

    # 需要把 archived_router 也挂上去
    from src.service.archived_router import router as archived_router
    app = _build_app(session_maker)
    app.include_router(archived_router)
    client = TestClient(app)
    token = _login(client)

    resp = client.get("/user/archived-sessions",
                      headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    data = resp.json()

    # by_project 列表里有 2 个 project（p1 + p2），且按 project_name 排序
    project_ids = [g["project_id"] for g in data["by_project"]]
    assert "p1" in project_ids
    assert "p2" in project_ids

    # 每个 project 里只有归档的 session（活动的不见）
    p1_group = next(g for g in data["by_project"] if g["project_id"] == "p1")
    p1_ids = [s["id"] for s in p1_group["sessions"]]
    assert "sess_a1_arch" in p1_ids
    assert "sess_a2_active" not in p1_ids


@pytest.mark.asyncio
async def test_list_archived_sessions_user_scoped(session_maker):
    """看不到其他用户的归档（按 current_user.id 过滤）。"""
    async with session_maker() as s:
        s.add(User(id=2, email="bob@x.com", username="bob",
                   hashed_password=sec.hash_password("12345678"),
                   is_active=True, is_admin=False))
        s.add(QASession(id="sess_bob", project_id="p1", user_id=2,
                        title="bob 的归档", message_count=1,
                        archived_at=datetime(2026, 5, 10, 0, 0, 0)))
        await s.commit()

    from src.service.archived_router import router as archived_router
    app = _build_app(session_maker)
    app.include_router(archived_router)
    client = TestClient(app)
    token = _login(client)  # alice

    resp = client.get("/user/archived-sessions",
                      headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    data = resp.json()
    # alice 看不到 bob 的
    all_session_ids = [s["id"] for g in data["by_project"] for s in g["sessions"]]
    assert "sess_bob" not in all_session_ids


# ───────── Task 7 测试 ─────────


@pytest.mark.asyncio
async def test_explain_to_archived_session_returns_409(session_maker):
    """对已归档 session POST /qa/explain → 409 Conflict。"""
    async with session_maker() as s:
        s.add(QASession(id="sess_arch", project_id="p1", user_id=1,
                        title="归档了",
                        archived_at=datetime(2026, 5, 1, 0, 0, 0)))
        await s.commit()

    app = _build_app(session_maker)
    client = TestClient(app)
    token = _login(client)

    resp = client.post(
        "/projects/p1/qa/explain",
        json={"question": "继续问", "session_id": "sess_arch"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 409
    detail = resp.json().get("detail", "")
    # 友好错误信息含「归档」字样，前端可据此提示
    assert "归档" in detail or "archived" in detail.lower()
