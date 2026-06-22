# tests/test_auth/test_qa_scm_gate.py
"""P4b-1 Task 3：QA /explain SCM can_query 门的集成测试。

测试矩阵：
  - flag_off_no_gate：KE_SCM_QA_AUTHZ 未设置 → 门不执行，authorize_scm 回调不被调用
  - flag_on_not_visible_403_no_session：flag 开 + NOT_VISIBLE → 返回 403（JSON，非流）
  - flag_on_not_visible_leaves_no_qasession：403 时不留孤儿 QASession
  - flag_on_unbound_skips：工程无 scm_connection_id → 跳过 SCM 门
  - flag_on_can_query_passes_gate：CAN_QUERY → 过门（引擎未就绪 503，不是 403）
  - flag_on_gitlab_connection_gate_fires：非 PAT 的 GitLab 连接也必须进门（I3/B1 回归）
"""
import pytest, pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select
from src.service import auth_security as sec
from src.service.auth_models import User
from src.service.auth_router import router as auth_router
from src.service.db import Base, get_db
from src.service.deps_infra import require_infra_healthy
from src.service.db_models_homepage import Project, ScmConnection, QASession
from src.service.qa_router import router as qa_router
from src.service.scm.base import ScmRole


@pytest_asyncio.fixture
async def session_maker(monkeypatch):
    # monkeypatch 设置测试用的 JWT 密钥（auth_security 从 env 读取）
    monkeypatch.setenv("KE_JWT_SECRET", "x" * 32)
    # 禁用 Secure cookie（测试环境是 http，不需要 https）
    monkeypatch.setenv("KE_COOKIE_SECURE", "false")
    # 创建内存 SQLite 异步引擎（每个 fixture 实例完全隔离，不影响其他测试）
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    # 同步创建所有表（Base.metadata 包含 User/Project/QASession/ScmConnection 等）
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    # async_sessionmaker：生成异步 Session 的工厂；expire_on_commit=False 防止 commit 后属性失效
    SM = async_sessionmaker(eng, expire_on_commit=False)
    # 插入测试数据
    async with SM() as s:
        # 管理员用户 alice
        s.add(User(email="a@x.com", username="alice", hashed_password=sec.hash_password("12345678"),
                   is_active=True, is_admin=True))
        # bound 工程 p1 + GitHub App 连接 c1（非 PAT，必须进门）
        s.add(Project(id="p1", name="P1", status="ready", scm_connection_id="c1",
                      repo_external_id=42, repo_full_name="o/r"))
        s.add(ScmConnection(id="c1", provider="github", auth_type="github_app",
                            github_installation_id=7, account_login="o", status="active", created_by="alice"))
        # unbound 工程 p2（scm_connection_id 为空 → 跳过 SCM 门）
        s.add(Project(id="p2", name="P2", status="ready"))
        # GitLab 绑定工程 pg + 连接 cg（非 PAT；I3/B1 回归用：GitLab 连接也要进门）
        s.add(Project(id="pg", name="PG", status="ready", scm_connection_id="cg",
                      repo_external_id=99, repo_full_name="g/r"))
        s.add(ScmConnection(id="cg", provider="gitlab", auth_type="github_app",
                            github_installation_id=None, account_login="g", status="active", created_by="alice"))
        await s.commit()
    return SM


def _client(session_maker, *, authorize_scm=None):
    """构造测试用 FastAPI 应用。

    - 挂载 auth_router（提供 /auth/login）和 qa_router（提供 /projects/{id}/qa/explain）
    - override get_db → 用内存 SQLite session_maker
    - override require_infra_healthy → 直接放行（不检查 Weaviate/Redis 等基础设施）
    - 可选：在 app.state.authorize_scm 上注入假的 authorize_scm 回调
    """
    app = FastAPI()
    # 挂载两个路由器（qa_router prefix=/projects/{project_id}/qa）
    app.include_router(auth_router)
    app.include_router(qa_router)

    # async 生成器：每次请求提供一个内存 DB session，请求结束后 commit
    async def override_db():
        async with session_maker() as s:
            yield s
            await s.commit()

    # FastAPI dependency override：用上面的 override_db 替换真实的 get_db
    app.dependency_overrides[get_db] = override_db
    # B2 注意：require_infra_healthy 是 router 级 dependency；
    # 若不 override，它在 SCM 门之前就会 503，导致门测试无法覆盖
    app.dependency_overrides[require_infra_healthy] = lambda: None

    # 可选：注入假的 authorize_scm 回调（gate 通过 app.state 读取）
    if authorize_scm is not None:
        app.state.authorize_scm = authorize_scm

    # TestClient：同步封装异步 FastAPI app（pytest 不需要 asyncio 的 HTTP 调用）
    return TestClient(app)


def _login(c):
    """登录 alice，返回 Authorization header dict。"""
    r = c.post("/auth/login", json={"username": "alice", "password": "12345678", "remember_me": False})
    assert r.status_code == 200, r.text
    # Bearer token 验证头：FastAPI 的 get_current_user dependency 读取此头
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _ask(c, pid, hdr):
    """向指定工程的 /qa/explain 发 POST 问答请求。

    B1 注意：真实路由前缀是 /projects/{id}/qa（qa_router prefix）+ /explain
    """
    return c.post(f"/projects/{pid}/qa/explain", json={"question": "什么是订单超时"}, headers=hdr)


def test_flag_off_no_gate(session_maker, monkeypatch):
    """KE_SCM_QA_AUTHZ 未设置时，门不执行（_boom 不被调）。

    过门后引擎未就绪 → 503（fixture 不挂 weaviate/synthesizer）。
    in (200,503) 同时排除 403（误触门）与 500（_boom 误调）。
    """
    # 删除 env var，确保 flag_on("KE_SCM_QA_AUTHZ") 返回 False
    monkeypatch.delenv("KE_SCM_QA_AUTHZ", raising=False)

    # _boom：如果 authorize_scm 被调用就抛出，证明门没有被执行
    async def _boom(*a, **k): raise AssertionError("gate should not run")
    c = _client(session_maker, authorize_scm=_boom)
    hdr = _login(c)
    # flag off → 不进门。过门后引擎未就绪 → 503（fixture 不挂引擎）
    # in (200,503) 同时排除 403(误触门) 与 500(_boom 误调)
    assert _ask(c, "p1", hdr).status_code in (200, 503)


def test_flag_on_not_visible_403_no_session(session_maker, monkeypatch):
    """门开启且 authorize_scm 返回 NOT_VISIBLE → 403 JSON 响应（非流）。"""
    monkeypatch.setenv("KE_SCM_QA_AUTHZ", "1")

    # 假的 authorize_scm：总是返回 NOT_VISIBLE（无权限）
    async def _authz(db, **k): return ScmRole.NOT_VISIBLE
    c = _client(session_maker, authorize_scm=_authz)
    hdr = _login(c)
    r = _ask(c, "p1", hdr)

    # 必须 403，content-type 是 JSON（不是 SSE 流）
    assert r.status_code == 403
    assert r.headers["content-type"].startswith("application/json")   # JSON 非流


@pytest.mark.asyncio
async def test_flag_on_not_visible_leaves_no_qasession(session_maker, monkeypatch):
    """403 必须在 QASession 创建之前触发 → DB 不留孤儿 session。

    用 pytest.mark.asyncio + session_maker 直接查 DB，验证 QASession 表为空。
    """
    monkeypatch.setenv("KE_SCM_QA_AUTHZ", "1")
    async def _authz(db, **k): return ScmRole.NOT_VISIBLE
    c = _client(session_maker, authorize_scm=_authz)
    hdr = _login(c)
    # 触发 403
    _ask(c, "p1", hdr)

    # 直接查内存 DB：p1 工程应该没有任何 QASession
    async with session_maker() as s:
        rows = (await s.execute(select(QASession).where(QASession.project_id == "p1"))).scalars().all()
    # 403 发生在 QASession 创建之前 → 无孤儿行
    assert rows == []


def test_flag_on_unbound_skips(session_maker, monkeypatch):
    """工程无 scm_connection_id（unbound）→ 跳过 SCM 门，_boom 不被调。

    unbound 工程只走 KE-RBAC，不需要 SCM 权限校验。
    """
    monkeypatch.setenv("KE_SCM_QA_AUTHZ", "1")

    # _boom 一旦被调就 fail，证明 unbound 工程不进门
    async def _boom(*a, **k): raise AssertionError("unbound should skip gate")
    c = _client(session_maker, authorize_scm=_boom)
    hdr = _login(c)
    # unbound p2 → 跳过 SCM 门（_boom 未被调）；引擎未就绪 → 503
    assert _ask(c, "p2", hdr).status_code in (200, 503)


def test_flag_on_can_query_passes_gate(session_maker, monkeypatch):
    """CAN_QUERY → 通过 SCM 门（进入引擎逻辑，引擎未就绪 503，不是 403）。

    注：过门后会先建 QASession 再 503，符合预期（不断言无孤儿）。
    """
    monkeypatch.setenv("KE_SCM_QA_AUTHZ", "1")

    # CAN_QUERY：有读取权限，应通过 SCM 门
    async def _authz(db, **k): return ScmRole.CAN_QUERY
    c = _client(session_maker, authorize_scm=_authz)
    hdr = _login(c)
    # 过门 → 引擎未就绪 503（fixture 不挂 weaviate/synthesizer）
    assert _ask(c, "p1", hdr).status_code in (200, 503)


def test_flag_on_gitlab_connection_gate_fires(session_maker, monkeypatch):
    """I3/B1 回归：非 PAT 的 GitLab 连接必须进门，不被当 PAT 跳过。

    fixture 已 seed pg（GitLab 工程）/cg（auth_type=github_app，非 PAT）。
    NOT_VISIBLE → 403 证明 GitLab 连接未被误判为 PAT 而跳过门。
    """
    monkeypatch.setenv("KE_SCM_QA_AUTHZ", "1")
    async def _authz(db, **k): return ScmRole.NOT_VISIBLE
    c = _client(session_maker, authorize_scm=_authz)
    hdr = _login(c)
    # GitLab 连接（非 PAT）进门 → NOT_VISIBLE → 403（证明未被 PAT 跳过）
    assert _ask(c, "pg", hdr).status_code == 403
