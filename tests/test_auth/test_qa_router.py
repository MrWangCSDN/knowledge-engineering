"""验证 /projects/{pid}/qa/explain SSE 路由。

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
    UserProjectAccess,  # v2.0 Task 5：RBAC 测试需要直接操作成员表
)
from src.service.project_router import router as project_router
from src.service.qa_engine.retriever import RetrievedContext
from src.service.qa_engine.router import SkillRouter
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
        # spec=['synthesize', 'llm']：限制 mock 属性集。
        #  - 'synthesize'：答案合成（保留）
        #  - 'llm'：_make_title_generator（会话标题特性）与 _make_memory_writer
        #    （记忆特性）都从 synthesizer.llm 取 LLM provider；缺它会 AttributeError
        # 仍不含 synthesize_stream，故 sse_emitter 不会误判"支持流式"。
        synthesizer = MagicMock(spec=['synthesize', 'llm'])
        synthesizer.synthesize = AsyncMock(return_value=SynthesizedAnswer(
            sections=[{"type": "overview", "title": "概述", "content": "答案", "references": []}],
            token_usage=100,
            cost_yuan=0.05,
        ))
        # 标题/记忆旁路用的 LLM。会话标题特性后，新会话标题由 _make_title_generator
        # 调 llm.complete 对“问题”做总结生成（不再截取问题前 N 字）。
        # 这里返回一个与测试问题相关的总结串，使 test_explain_persists_* 的
        # “标题含问题关键词”断言在新行为下仍有效（compaction 因消息数未达阈值不触发）。
        synthesizer.llm = AsyncMock()
        synthesizer.llm.complete = AsyncMock(return_value="存款开户设计逻辑")
    app.state.qa_retriever = retriever
    app.state.qa_synthesizer = synthesizer
    # 真实 SkillRouter（关键词路径无依赖，注入零成本）
    # 不 mock，因为 router 自己是个纯函数，比 mock 更接近真实行为
    app.state.qa_router = SkillRouter()
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
        "/projects/nonexistent/qa/explain",
        headers=_auth(token),
        json={"question": "x"},
    )
    assert r.status_code == 404


def test_explain_409_project_indexing(client, seed_indexing_project):
    token = _login(client)
    r = client.post(
        f"/projects/{seed_indexing_project}/qa/explain",
        headers=_auth(token),
        json={"question": "x"},
    )
    assert r.status_code == 409


def test_explain_requires_auth(client, seed_ready_project):
    r = client.post(
        f"/projects/{seed_ready_project}/qa/explain",
        json={"question": "x"},
    )
    assert r.status_code == 401


def test_explain_validates_question_not_empty(client, seed_ready_project):
    token = _login(client)
    r = client.post(
        f"/projects/{seed_ready_project}/qa/explain",
        headers=_auth(token),
        json={"question": ""},
    )
    assert r.status_code == 422


# ───────── 正常 SSE 流 ─────────

def test_explain_returns_sse_content_type(client, seed_ready_project):
    token = _login(client)
    r = client.post(
        f"/projects/{seed_ready_project}/qa/explain",
        headers=_auth(token),
        json={"question": "存款开户"},
    )
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]


def test_explain_sse_events_in_order(client, seed_ready_project):
    token = _login(client)
    with client.stream(
        "POST",
        f"/projects/{seed_ready_project}/qa/explain",
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


# ───────── 路由：meta 事件带 skill 决策 ─────────


def test_explain_meta_event_includes_skill_decision(client, seed_ready_project):
    """meta 事件的 data 应该带 skill_id + route_source，便于前端展示『识别为 xxx 类问题』。

    用 dependency 关键词的问题，验证 skill_id == 'dependency'。
    """
    import json as _json  # 避免和模块顶部某些 json 字符串冲突

    token = _login(client)
    with client.stream(
        "POST",
        f"/projects/{seed_ready_project}/qa/explain",
        headers=_auth(token),
        # "调用" 命中 dependency 关键词
        json={"question": "OwnerController 调用了哪些方法？"},
    ) as r:
        body = "".join(r.iter_text())

    # SSE 帧格式：`event: <type>\ndata: <json>\n\n`
    # 找到 meta 行后面紧跟的 data: 行
    lines = body.split("\n")
    meta_line_idx = next(i for i, l in enumerate(lines) if l == "event: meta")
    # data: 行紧跟在 event: 行之后
    data_line = lines[meta_line_idx + 1]
    assert data_line.startswith("data: ")
    meta_payload = _json.loads(data_line[len("data: "):])

    assert meta_payload.get("skill_id") == "dependency"
    # source 应该是 'keyword'（关键词命中而不是 LLM）
    assert meta_payload.get("route_source") == "keyword"


# ───────── 持久化 ─────────

@pytest.mark.asyncio
async def test_explain_persists_user_and_assistant_messages(session_maker, seed_ready_project):
    """问完一次后 qa_sessions + qa_messages 表应该有 1 个 session 和 2 条消息。"""
    app = _build_app(session_maker)
    client = TestClient(app)
    token = _login(client)

    with client.stream(
        "POST",
        f"/projects/{seed_ready_project}/qa/explain",
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


# ───────── v1.5 docx 导出 ─────────


@pytest.mark.asyncio
async def test_export_message_as_docx(session_maker, seed_ready_project):
    """GET /sessions/{sid}/messages/{mid}/export?format=docx
       → 返回 200 + word docx binary + Content-Disposition: attachment。
    """
    app = _build_app(session_maker)
    client = TestClient(app)
    token = _login(client)

    # 1) 先发一个问题让 DB 里有 user + assistant 消息
    with client.stream(
        "POST",
        f"/projects/{seed_ready_project}/qa/explain",
        headers=_auth(token),
        json={"question": "测试问题"},
    ) as r:
        "".join(r.iter_text())  # 消费完触发持久化

    # 2) 找 assistant 消息的 id
    async with session_maker() as db:
        msgs = (await db.execute(
            select(QAMessage).where(QAMessage.role == "assistant")
        )).scalars().all()
        assert len(msgs) == 1
        assistant_msg = msgs[0]
        session_id = assistant_msg.session_id

    # 3) 调导出 endpoint
    r = client.get(
        f"/projects/{seed_ready_project}/qa/sessions/{session_id}/messages/{assistant_msg.id}/export",
        headers=_auth(token),
        params={"format": "docx"},
    )
    assert r.status_code == 200
    # MIME 类型应该是 Word
    assert "officedocument.wordprocessingml" in r.headers["content-type"]
    # Content-Disposition 应该是 attachment + filename
    cd = r.headers.get("content-disposition", "")
    assert "attachment" in cd.lower()
    assert ".docx" in cd

    # 内容应该是合法 docx（zip 起头 PK）
    assert r.content[:2] == b"PK"
    assert len(r.content) > 1000  # 远大于一个最简单文档


@pytest.mark.asyncio
async def test_export_404_when_message_not_found(session_maker, seed_ready_project):
    """不存在的 message_id → 404。"""
    app = _build_app(session_maker)
    client = TestClient(app)
    token = _login(client)
    r = client.get(
        f"/projects/{seed_ready_project}/qa/sessions/fake-sess/messages/fake-msg/export",
        headers=_auth(token),
        params={"format": "docx"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_export_rejects_unsupported_format(session_maker, seed_ready_project):
    """目前只支持 docx；传 pdf → 400。"""
    app = _build_app(session_maker)
    client = TestClient(app)
    token = _login(client)
    # 不必有真消息也能触发 format 校验（在解析参数阶段就拒）
    r = client.get(
        f"/projects/{seed_ready_project}/qa/sessions/x/messages/y/export",
        headers=_auth(token),
        params={"format": "pdf"},
    )
    assert r.status_code == 400


# ───────── v2.0 Task 5：require_project_role RBAC 校验 ─────────

# 以下两个测试专门验证非 admin 路径下的 RBAC 行为：
#   - 非 admin + 未加入工程 → 403
#   - 非 admin + 加为 reporter → 200
# 注意：alice 默认是 admin（is_admin=True），既有测试走的全是 admin 路径，
# 加了 require_project_role 后 admin 路径不受影响（is_admin=True → resolve_role 直接返回 owner）。


def test_explain_403_when_user_not_project_member(client, seed_ready_project, session_maker):
    """非 admin 用户、未加入工程 → /qa/explain 应 403。

    步骤：
      1. 把 alice 的 is_admin 降为 False（让她成为普通用户）
      2. 不向 user_project_access 插入记录（alice 对工程无任何成员关系）
      3. 请求 /qa/explain → 期望 403
    """
    import asyncio
    # sqlalchemy 的 update 函数：生成 UPDATE 语句
    from sqlalchemy import update

    # ── 把 alice 降为普通用户 ────────────────────────────────────────────────
    async def remove_admin():
        """异步辅助：在独立 session 里把 is_admin 改为 False。"""
        # async with session_maker() as s：上下文管理器，自动关闭 session（同时处理异常）
        async with session_maker() as s:
            # update(User).where(...).values(...)：构造 UPDATE users SET is_admin=false WHERE username='alice'
            await s.execute(
                update(User).where(User.username == "alice").values(is_admin=False)
            )
            await s.commit()  # 提交事务，写入数据库

    # asyncio.get_event_loop().run_until_complete(coro)：
    #   在同步函数里执行异步协程的标准做法（TestClient 的测试函数是同步的，不能直接 await）
    asyncio.get_event_loop().run_until_complete(remove_admin())

    # ── alice 此时：既非 admin，又不是工程成员 → resolve_role 返 None → 403 ─
    token = _login(client)
    r = client.post(
        f"/projects/{seed_ready_project}/qa/explain",
        headers=_auth(token),
        json={"question": "x"},
    )
    # require_project_role 加上之前：此处会返回 200（旧行为）
    # require_project_role 加上之后：应该返回 403（新的正确行为）
    assert r.status_code == 403, f"期望 403，实际 {r.status_code}: {r.text}"


@pytest.mark.asyncio
async def test_explain_meta_context_usage_when_history_trimmed(
    session_maker, seed_ready_project, monkeypatch
):
    """端到端：router 压力块（trim_history_to_budget → context_usage → SSE meta）接线验证。

    通过把 KE_MODEL_CONTEXT_WINDOW 设成刚好到达 _MIN_WINDOW 下限的值（1000 token），
    确保 8 条 200 字的历史消息超过预算，触发真实裁史，meta 事件携带 context_usage +
    history_trimmed:true + window_tokens 字段。

    Budget 算法（window=1000）：
      history_token_budget() = int(1000 * (1 - 0.45)) = int(550) = 550 tokens
      8 messages × ceil(200/1.5)=134 tokens = 1072 tokens >> 550 → 裁史必触发

    注：SessionCompactor + read_session_summary 被 patch 为 no-op（S5 后），
    避 SQLite 测试 DB 与真 fs 路径耦合（本测试目的是验 router 压力块→meta 接线，
    不测压缩 / composer 本身；专项覆盖由 test_memory_session.py 提供）。
    """
    import json as _json

    monkeypatch.setenv("KE_MODEL_CONTEXT_WINDOW", "1000")
    # patch SessionCompactor 类本身：让 SessionCompactor(llm).compact(...) 整体 no-op
    # S5 闭包内 lazy import，patch 真正符号位置 src.service.memory.session
    class _NoopCompactor:
        def __init__(self, llm):
            pass

        async def compact(self, *args, **kw):
            return None

    monkeypatch.setattr(
        "src.service.memory.session.SessionCompactor",
        _NoopCompactor,
    )
    # 同时 patch read_session_summary 为 no-op（5b 读侧也用文件，SQLite 测试 DB
    # 与文件 fs 独立，理论上 read 不存在路径会返 ""，但显式 patch 避免依赖文件状态）
    async def _empty_read(*args, **kw):
        return ""
    monkeypatch.setattr(
        "src.service.memory.session.read_session_summary",
        _empty_read,
    )

    app = _build_app(session_maker)
    client = TestClient(app)
    token = _login(client)

    # 8 条历史消息，每条内容 200 字：
    #   history_token_budget = int(1000 * (1 - 0.45)) = 550 tokens
    #   每条估算 = ceil(200/1.5) = 134 tokens
    #   8 × 134 = 1072 tokens >> 550 → 裁史必触发
    long_content = "A" * 200
    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": long_content}
        for i in range(8)
    ]

    with client.stream(
        "POST",
        f"/projects/{seed_ready_project}/qa/explain",
        headers=_auth(token),
        json={"question": "存款开户流程？", "history": history},
    ) as r:
        body = "".join(r.iter_text())

    assert "event: meta" in body, f"meta 事件未找到，body={body[:500]}"

    lines = body.split("\n")
    meta_line_idx = next(
        (i for i, l in enumerate(lines) if l.strip() == "event: meta"), None
    )
    assert meta_line_idx is not None, "meta 事件行未找到"
    data_line = lines[meta_line_idx + 1]
    assert data_line.startswith("data: "), f"meta 后 data 行格式错误：{data_line!r}"
    meta_payload = _json.loads(data_line[len("data: "):])

    # 核心断言：context_usage 存在且携带裁史信息
    assert "context_usage" in meta_payload, f"meta 缺 context_usage：{meta_payload}"
    cu = meta_payload["context_usage"]
    assert cu.get("history_trimmed") is True, f"history_trimmed 应为 true，实际：{cu}"
    assert "window_tokens" in cu, f"context_usage 缺 window_tokens：{cu}"


def test_explain_200_when_user_is_project_member(client, seed_ready_project, session_maker):
    """非 admin 用户 + user_project_access 加为 reporter → 应能正常问答（200）。

    步骤：
      1. 把 alice 的 is_admin 降为 False
      2. 向 user_project_access 插入 alice→seed_ready_project 的 reporter 成员记录
      3. 请求 /qa/explain → 期望 200（SSE 流）
    """
    import asyncio
    from sqlalchemy import select, update

    # ── 降级 alice + 注册为 reporter ────────────────────────────────────────
    async def setup():
        """异步辅助：降级 + 插入成员记录（在同一个 session 里保持事务一致性）。"""
        async with session_maker() as s:
            # step 1：把 alice 改为非 admin
            await s.execute(
                update(User).where(User.username == "alice").values(is_admin=False)
            )
            # step 2：查出 alice 的 id（UserProjectAccess 需要整数 user_id）
            # select(User).where(...)：构造 SELECT * FROM users WHERE username='alice'
            # scalar_one()：期望恰好一条结果，若零条 / 多条都会抛出异常
            user = (
                await s.execute(select(User).where(User.username == "alice"))
            ).scalar_one()

            # step 3：插入 reporter 成员记录
            # UserProjectAccess(user_id=..., project_id=..., role=...)：
            #   构造 ORM 对象，s.add() 把它放入 session 的待插入队列
            s.add(
                UserProjectAccess(
                    user_id=user.id,
                    project_id=seed_ready_project,
                    role="reporter",  # 最低权限：只读，可以访问 qa
                )
            )
            await s.commit()  # 一次性提交：is_admin 更新 + 成员插入同在一个事务里

    asyncio.get_event_loop().run_until_complete(setup())

    # ── alice 此时：非 admin，但是工程 reporter → resolve_role 返 'reporter' ≥ 'reporter' → 200
    token = _login(client)
    r = client.post(
        f"/projects/{seed_ready_project}/qa/explain",
        headers=_auth(token),
        json={"question": "x"},
    )
    # mock retriever / synthesizer 已在 _build_app 中注入，会返回 SSE 流（200）
    assert r.status_code == 200, f"期望 200，实际 {r.status_code}: {r.text}"
