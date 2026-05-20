"""验证 /projects/{pid}/qa/sessions 系列路由。

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
    """seed: 1 个 alice 的 session（S6 后消息改写 fs；此 fixture 只创建 DB session 元数据）。

    S6 改造（§7.4）：删 QAMessage/QAFeedback DB 插入（表已删）。
    message_count 保留初始值 2（模拟外部写入 fs 后的状态）。
    """
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
        await s.commit()
    return "sess_alice_1"


# ───────── GET /sessions ─────────

def test_list_sessions_empty(client):
    token, _ = _login(client)
    r = client.get("/projects/deposit/qa/sessions", headers=_auth(token))
    assert r.status_code == 200
    assert r.json() == {"sessions": []}


def test_list_sessions_with_data(client, seeded_session):
    token, _ = _login(client)
    r = client.get("/projects/deposit/qa/sessions", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert len(body["sessions"]) == 1
    assert body["sessions"][0]["id"] == seeded_session
    assert body["sessions"][0]["title"] == "存款开户的设计逻辑"
    assert body["sessions"][0]["message_count"] == 2


def test_list_sessions_filters_by_user(client, seeded_session):
    """bob 未加入工程 → 被 require_project_role("reporter") 拦截，返回 403。

    v2.0 RBAC 行为变更：
      v1.x：bob 能访问 endpoint，返回空列表（按 user_id 过滤）
      v2.0：bob 不是工程成员，连 endpoint 都进不去 → 403
    这更安全：非成员无法探知工程下是否存在任何 QA 数据。
    """
    token, _ = _login(client, username="bob")
    r = client.get("/projects/deposit/qa/sessions", headers=_auth(token))
    # require_project_role 先于路由函数执行；bob 无成员关系 → HTTPException(403)
    assert r.status_code == 403


def test_list_sessions_requires_auth(client):
    r = client.get("/projects/deposit/qa/sessions")
    assert r.status_code == 401


# ───────── GET /sessions/{sid} ─────────

def test_get_session_with_messages(client, seeded_session):
    """GET /sessions/{sid} 返回 session 元数据 + fs 消息列表。

    S6 改造（§7.4）：消息改读 fs；测试环境无 fs 写入 → messages=[]。
    断言 session 元数据结构正确（id/project_id/title/message_count）。
    """
    token, _ = _login(client)
    r = client.get(
        f"/projects/deposit/qa/sessions/{seeded_session}",
        headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["session"]["id"] == seeded_session
    assert body["session"]["title"] == "存款开户的设计逻辑"
    assert body["session"]["message_count"] == 2
    # S6：fs 读侧在测试环境无文件 → 返 []（read_messages_for_session 目录不存在时返空）
    assert isinstance(body["messages"], list)


def test_get_session_404(client):
    token, _ = _login(client)
    r = client.get("/projects/deposit/qa/sessions/no-such", headers=_auth(token))
    assert r.status_code == 404


def test_get_session_other_user_forbidden(client, seeded_session):
    """bob 未加入工程 → 403（被 require_project_role 拦截，无法获取他人会话）。

    v2.0 RBAC 行为变更：
      v1.x：bob 能进 endpoint，但因 sess.user_id != bob.id 而 404
      v2.0：bob 不是工程成员，在 endpoint 入口处就被 403 拒绝
    结果：非成员既无法猜测会话 ID，也无法确认会话是否存在。
    """
    token, _ = _login(client, username="bob")
    r = client.get(
        f"/projects/deposit/qa/sessions/{seeded_session}",
        headers=_auth(token),
    )
    # require_project_role 先执行；bob 非工程成员 → 403，路由函数体不会被执行
    assert r.status_code == 403


# ───────── DELETE /sessions/{sid} ─────────

def test_delete_session_cascades_messages(client, seeded_session, session_maker):
    """delete_session 删 DB row 后 fs 目录级联清理（§8.3 S7 修）。

    S6 → S7 路径：S6 后 fs message 文件成 orphan；S7 加 fs.rm recursive 级联
    清理整目录。本测试保留 DB delete 主断言；fs 级联断言由
    test_delete_session_cascades_fs_cleanup 专项覆盖。
    """
    token, _ = _login(client)
    r = client.delete(
        f"/projects/deposit/qa/sessions/{seeded_session}",
        headers=_auth(token),
    )
    assert r.status_code == 204

    # 验证 DB 状态：session 没了
    import asyncio
    async def _check():
        async with session_maker() as db:
            sessions = (await db.execute(select(QASession))).scalars().all()
            assert len(sessions) == 0
    asyncio.run(_check())


def test_delete_session_404(client):
    token, _ = _login(client)
    r = client.delete("/projects/deposit/qa/sessions/no-such", headers=_auth(token))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_session_cascades_fs_cleanup(client, seeded_session, monkeypatch, tmp_path):
    """delete_session 调 fs.rm 级联清理 session 目录（§8.3 S7）。

    先 fs.write session 目录下若干文件（summary + messages），调 DELETE endpoint，
    断言 fs 目录消失。MemoryFS root 指向 tmp_path 避污染仓库 .ke-memory/。
    """
    # MemoryFS 默认 root 由 KE_MEM_ROOT env 派生；测试期指向 tmp_path
    monkeypatch.setenv("KE_MEM_ROOT", str(tmp_path))

    # 准备 fs：先写 session 目录下若干文件
    from src.service.memory.vfs import MemoryFS
    fs = MemoryFS()
    # seeded_session 返回 session_id 字符串；user_id 经 _login 获取
    session_id = seeded_session
    token, user_id = _login(client)
    await fs.write(
        f"ke://u/{user_id}/session/{session_id}/summary.md",
        "---\nturn_count: 2\n---\n会话摘要\n",
    )
    await fs.write(
        f"ke://u/{user_id}/session/{session_id}/messages/msg_test_a.md",
        "---\nrole: user\ncreated_at: \"2026-05-22T10:00:00Z\"\n---\nuser msg\n",
    )
    # S7 holistic minor 2：补 .feedback.md sibling 也被 fs.rm recursive 级联清
    await fs.write(
        f"ke://u/{user_id}/session/{session_id}/messages/msg_test_a.feedback.md",
        "---\nvote: up\nuser_id: " + str(user_id) + "\ncreated_at: \"2026-05-22T10:00:00Z\"\n---\nfeedback comment\n",
    )

    # 调 DELETE endpoint
    r = client.delete(
        f"/projects/deposit/qa/sessions/{session_id}",
        headers=_auth(token),
    )
    assert r.status_code == 204

    # 验证 fs 目录消失
    assert not await fs.exists(f"ke://u/{user_id}/session/{session_id}/summary.md")
    assert not await fs.exists(f"ke://u/{user_id}/session/{session_id}/messages/msg_test_a.md")
    assert not await fs.exists(f"ke://u/{user_id}/session/{session_id}/messages/msg_test_a.feedback.md")


@pytest.mark.asyncio
async def test_delete_session_fs_not_exists_ok(client, seeded_session, monkeypatch, tmp_path):
    """delete_session 在 fs 目录不存在时不抛（首压前 session / 仅 DB row 无 fs，§8.3）。

    seeded_session fixture 仅 seed DB 不 seed fs；调 DELETE endpoint 应 204
    （MemoryNotFound 被中层 catch + continue）。
    """
    monkeypatch.setenv("KE_MEM_ROOT", str(tmp_path))
    session_id = seeded_session
    token, user_id = _login(client)

    # fs 目录从未创建（fixture 仅 DB seed）
    from src.service.memory.vfs import MemoryFS
    fs = MemoryFS()
    assert not await fs.exists(f"ke://u/{user_id}/session/{session_id}")

    r = client.delete(
        f"/projects/deposit/qa/sessions/{session_id}",
        headers=_auth(token),
    )
    assert r.status_code == 204


def test_delete_other_user_forbidden(client, seeded_session):
    """bob 未加入工程 → 403（被 require_project_role 拦截，无法删除他人会话）。

    v2.0 RBAC 行为变更：
      v1.x：bob 能进 endpoint，但因 sess.user_id != bob.id 而 404
      v2.0：bob 不是工程成员，在 endpoint 入口处就被 403 拒绝
    安全性提升：非成员连"会话是否存在"的信息也无法探测。
    """
    token, _ = _login(client, username="bob")
    r = client.delete(
        f"/projects/deposit/qa/sessions/{seeded_session}",
        headers=_auth(token),
    )
    # require_project_role 先执行；bob 非工程成员 → 403
    assert r.status_code == 403


# ───────── POST /sessions/{sid}/messages/{mid}/feedback ─────────

@pytest.mark.asyncio
async def test_post_feedback_success(client, seeded_session, monkeypatch, tmp_path):
    """post_feedback 写 fs feedback file 成功（§8.4 S7 修 S6 broken stub）。

    S6 后是 404 stub；S7 恢复完整契约：POST upvote → fs.read 看 frontmatter.vote=up。
    """
    monkeypatch.setenv("KE_MEM_ROOT", str(tmp_path))
    session_id = seeded_session
    token, user_id = _login(client)
    message_id = "msg_test_assistant"

    r = client.post(
        f"/projects/deposit/qa/sessions/{session_id}/messages/{message_id}/feedback",
        json={"vote": "up", "comment": "答得好"},
        headers=_auth(token),
    )
    assert r.status_code == 204

    # 验 fs 写入
    from src.service.memory.vfs import MemoryFS
    from src.service.memory.memgen import _split_frontmatter
    fs = MemoryFS()
    raw = await fs.read(
        f"ke://u/{user_id}/session/{session_id}/messages/{message_id}.feedback.md"
    )
    fm, body = _split_frontmatter(raw)
    assert fm["vote"] == "up"
    assert fm["user_id"] == user_id
    assert body.strip() == "答得好"


@pytest.mark.asyncio
async def test_post_feedback_overwrites_existing(client, seeded_session, monkeypatch, tmp_path):
    """post_feedback 同 msg_id 第二次覆盖第一次（fs.write 原子覆盖，§8.4）。"""
    monkeypatch.setenv("KE_MEM_ROOT", str(tmp_path))
    session_id = seeded_session
    token, user_id = _login(client)
    message_id = "msg_test_overwrite"

    # 第一次 up
    r1 = client.post(
        f"/projects/deposit/qa/sessions/{session_id}/messages/{message_id}/feedback",
        json={"vote": "up"},
        headers=_auth(token),
    )
    assert r1.status_code == 204
    # 第二次 down（覆盖）
    r2 = client.post(
        f"/projects/deposit/qa/sessions/{session_id}/messages/{message_id}/feedback",
        json={"vote": "down", "comment": "改主意了"},
        headers=_auth(token),
    )
    assert r2.status_code == 204

    # 验最终态 = down
    from src.service.memory.vfs import MemoryFS
    from src.service.memory.memgen import _split_frontmatter
    fs = MemoryFS()
    raw = await fs.read(
        f"ke://u/{user_id}/session/{session_id}/messages/{message_id}.feedback.md"
    )
    fm, body = _split_frontmatter(raw)
    assert fm["vote"] == "down"
    assert body.strip() == "改主意了"


@pytest.mark.asyncio
async def test_post_feedback_unknown_message_writes_orphan_feedback(client, seeded_session, monkeypatch, tmp_path):
    """post_feedback 对未知 message_id 仍返 204 写 orphan feedback file（§8.4 设计选择）。

    S7 endpoint 不校验 message 在 fs 真实存在 — 写 feedback file 是独立操作。
    Orphan feedback file 在 delete_session §8.3 时一同被 fs.rm recursive 清。
    """
    monkeypatch.setenv("KE_MEM_ROOT", str(tmp_path))
    session_id = seeded_session
    token, user_id = _login(client)
    message_id = "msg_does_not_exist"

    r = client.post(
        f"/projects/deposit/qa/sessions/{session_id}/messages/{message_id}/feedback",
        json={"vote": "up"},
        headers=_auth(token),
    )
    assert r.status_code == 204

    # orphan feedback file 已写
    from src.service.memory.vfs import MemoryFS
    fs = MemoryFS()
    assert await fs.exists(
        f"ke://u/{user_id}/session/{session_id}/messages/{message_id}.feedback.md"
    )


def test_post_feedback_validates_vote(client, seeded_session):
    """vote 只能是 up / down（Pydantic 422 在 endpoint 体执行前触发）。

    S6 注：post_feedback 已暂返 404 stub（qa_feedback 表已删，S7 迁文件后恢复完整契约）；
    本测试验证的是 Pydantic body validation（vote enum 校验），独立于 endpoint 体 stub
    状态，未来 S7 恢复完整契约后本测试断言不变（仍 422）。

    S7 注：Pydantic body validation 422 与 S7 endpoint 200/500 行为正交 —
    Pydantic 先于 endpoint 体执行；vote="maybe" 仍 422，不取决于 fs 状态。
    """
    token, _ = _login(client)
    r = client.post(
        f"/projects/deposit/qa/sessions/{seeded_session}/messages/msg_a_1/feedback",
        headers=_auth(token),
        json={"vote": "maybe"},  # 非法
    )
    assert r.status_code == 422


# ───────── PATCH /sessions/{sid}（重命名）─────────

class TestRenameSession:
    """PATCH /projects/{pid}/qa/sessions/{sid} 重命名。"""

    def test_rename_ok_sets_title_custom(self, client, seeded_session):
        token, _ = _login(client)
        r = client.patch(
            f"/projects/deposit/qa/sessions/{seeded_session}",
            json={"title": "我的自定义标题"},
            headers=_auth(token),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["title"] == "我的自定义标题"
        assert body["title_custom"] is True

    def test_rename_empty_title_400(self, client, seeded_session):
        token, _ = _login(client)
        r = client.patch(
            f"/projects/deposit/qa/sessions/{seeded_session}",
            json={"title": "   "},
            headers=_auth(token),
        )
        assert r.status_code == 400

    def test_rename_too_long_400(self, client, seeded_session):
        token, _ = _login(client)
        r = client.patch(
            f"/projects/deposit/qa/sessions/{seeded_session}",
            json={"title": "x" * 101},
            headers=_auth(token),
        )
        assert r.status_code == 400

    def test_rename_unknown_session_404(self, client, seeded_session):
        token, _ = _login(client)
        r = client.patch(
            "/projects/deposit/qa/sessions/sess_doesnotexist",
            json={"title": "x"},
            headers=_auth(token),
        )
        assert r.status_code == 404

    def test_rename_archived_session_409(self, client, seeded_session, session_maker):
        token, _ = _login(client)

        # 直接把 session 标记归档（沿用本文件 asyncio.run + session_maker 约定）
        import asyncio
        from datetime import datetime

        async def _archive():
            async with session_maker() as db:
                s = (await db.execute(
                    select(QASession).where(QASession.id == seeded_session)
                )).scalar_one()
                s.archived_at = datetime.utcnow()
                await db.commit()
        asyncio.run(_archive())

        r = client.patch(
            f"/projects/deposit/qa/sessions/{seeded_session}",
            json={"title": "改个名"},
            headers=_auth(token),
        )
        assert r.status_code == 409
