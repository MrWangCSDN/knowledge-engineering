"""POST /scm/connections/{connection_id}/projects 端点的集成测试。

验证：
  1. 连接不存在 → 404
  2. alice 用 bob 的连接 → 404（归属保护，不泄露他人连接存在性）
  3. App 连接 + authorize_scm 返 CAN_QUERY → 403（需要 can_bind）
  4. PAT 连接跳过 SCM 门，authorize_scm 设为 deny → 不报 403（拿到 4xx 之前先确认非 403）
  5. project_id 已存在 → 409
  6. 正常路径 → 200 + DB 里建好 Project/UserProjectAccess(owner)/IndexJob(full_index)
  7. 错误路径（404/403/409）→ 零写入断言

照 test_scm_binding_rbac.py 的 session_maker + client + _login 范式。
"""
from __future__ import annotations

import pytest              # 测试框架，提供 @pytest.fixture 等装饰器
import pytest_asyncio      # 提供 @pytest_asyncio.fixture 支持异步 fixture

from fastapi import FastAPI      # 构造测试用 mini-app
from fastapi.testclient import TestClient  # 把 async FastAPI 包成同步测试客户端

from sqlalchemy import select    # SQLAlchemy Core 查询 DSL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # 异步 ORM 引擎

# ── 鉴权辅助 ────────────────────────────────────────────────────────────────
from src.service import auth_security as sec   # hash_password —— 生成 bcrypt 密文
from src.service.auth_models import User       # 用户 ORM 模型（users 表）
from src.service.auth_router import router as auth_router  # 提供 /auth/login 路由

# ── DB 层 ────────────────────────────────────────────────────────────────────
from src.service.db import Base, get_db        # Base 用于建表；get_db 是 FastAPI dependency

# ORM 模型：Project 工程元数据、ScmConnection SCM 连接、UserProjectAccess 权限、IndexJob 索引作业
from src.service.db_models_homepage import (
    Project, ScmConnection, UserProjectAccess, IndexJob
)

# ── 被测路由 + SCM 枚举 ────────────────────────────────────────────────────────
from src.service.scm_binding_router import create_scm_binding_routes  # 路由工厂
from src.service.scm.base import ScmRole      # CAN_BIND / CAN_QUERY / NOT_VISIBLE

# ── get_current_user：与 api.py 一致，从 JWT cookie/header 解析用户 ──────────────
from src.service.auth_dependencies import get_current_user


# ─── 内存 DB fixture（seed：alice / bob / 三条连接 / 已有工程 taken） ───────────

@pytest_asyncio.fixture
async def session_maker(monkeypatch):
    """内存 SQLite + seed 数据。

    Alice 是普通用户（is_admin=False）—— 测试 create_project 时不依赖 admin 旁路。
    Seed：
      - alice（普通用户）、bob（普通用户）
      - c1   : alice 的 github_app 连接
      - c2   : bob  的 github_app 连接
      - cpat : alice 的 pat 连接
      - Project(id="taken")：用于 409 collision 测试
    """
    # monkeypatch.setenv：测试期间临时设置环境变量，用例结束后自动还原
    monkeypatch.setenv("KE_JWT_SECRET", "x" * 32)   # JWT 签名密钥（测试专用，任意 32 字节）
    monkeypatch.setenv("KE_COOKIE_SECURE", "false")  # 关掉 HTTPS-only cookie 检查

    # create_async_engine：创建异步 SQLite 引擎；":memory:" = 每次都是全新内存库
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    # async with eng.begin() as conn：开一个隐式事务上下文
    # run_sync(Base.metadata.create_all)：DDL（建表）在同步上下文里执行
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # async_sessionmaker：SQLAlchemy 2.0 的异步 Session 工厂
    # expire_on_commit=False：提交后 ORM 对象不过期，避免后续访问触发 DetachedInstanceError
    SM = async_sessionmaker(eng, expire_on_commit=False)

    # ── seed 数据 ──
    async with SM() as s:
        # alice 是普通用户（is_admin=False），这样测试不依赖 admin 捷径
        s.add(User(
            email="alice@x.com", username="alice",
            hashed_password=sec.hash_password("12345678"),
            is_active=True, is_admin=False,  # 注意：False，确保走真实逻辑
        ))
        # bob 是普通用户，拥有自己的连接 c2
        s.add(User(
            email="bob@x.com", username="bob",
            hashed_password=sec.hash_password("12345678"),
            is_active=True, is_admin=False,
        ))
        # c1：alice 拥有的 github_app 连接（非 pat → 走 SCM 授权门）
        s.add(ScmConnection(
            id="c1", created_by="alice",
            provider="github", auth_type="github_app",
        ))
        # c2：bob 拥有的 github_app 连接（alice 不能用）
        s.add(ScmConnection(
            id="c2", created_by="bob",
            provider="github", auth_type="github_app",
        ))
        # cpat：alice 拥有的 PAT 连接（auth_type="pat" → 跳过 SCM 授权门）
        s.add(ScmConnection(
            id="cpat", created_by="alice",
            provider="github", auth_type="pat",
        ))
        # Project(id="taken")：已存在的工程，用于 409 collision 测试
        s.add(Project(id="taken", name="已占用工程"))
        # s.commit()：提交事务，数据真正落库（内存库）
        await s.commit()

    # 返回 session 工厂，供 client fixture 使用
    return SM


# ─── 辅助：构造 client fixture ─────────────────────────────────────────────────

def _make_client(session_maker, authorize_scm_fn):
    """内部工厂：构造带指定 authorize_scm 的 TestClient。

    Args:
        session_maker: 内存 DB 的 async_sessionmaker，由 session_maker fixture 提供
        authorize_scm_fn: 注入给路由工厂的 fake authorize_scm 函数（可控制返回的 ScmRole）
    """
    # 创建 mini FastAPI app，只挂载测试需要的路由
    app = FastAPI()
    # 挂载 /auth/login 路由（login 需要它来颁发 JWT）
    app.include_router(auth_router)
    # 挂载被测路由；注入 fake authorize_scm，不用真实 GitHub API
    app.include_router(create_scm_binding_routes(
        get_current_user=get_current_user,
        get_db=get_db,
        authorize_scm=authorize_scm_fn,  # 注入 fake（让测试控制 SCM 门返回值）
    ))

    # dependency_overrides：FastAPI 测试钩子，把生产 get_db 替换成内存 DB
    async def override_db():
        """替换 get_db → 使用内存 SQLite session。"""
        # async with session_maker() as s：进入 session 上下文（自动关闭）
        async with session_maker() as s:
            yield s           # 把 session 交给 FastAPI 依赖注入系统
            await s.commit()  # 测试结束后提交

    # 用字典覆写 dependency，key 必须是 get_db 函数本身（不是字符串）
    app.dependency_overrides[get_db] = override_db
    # TestClient：把 async ASGI app 包装成同步接口，方便用 assert 检查 HTTP 响应
    return TestClient(app)


# ─── 两个 fixture：默认 CAN_BIND / 可配置 role ────────────────────────────────

@pytest.fixture
def client(session_maker):
    """默认 client：authorize_scm 始终返回 CAN_BIND（模拟 maintainer 身份）。

    大多数测试用这个 fixture，避免 SCM 门误拦。
    """
    # 定义一个 fake authorize_scm，始终返回 CAN_BIND
    async def fake_authorize_scm(db, *, user, conn, repo_full_name, repo_external_id, need_bind):
        """Fake：无论什么仓，都返回 CAN_BIND（最高权限）。"""
        # CAN_BIND = 可以绑定（maintainer/admin 级别）
        return ScmRole.CAN_BIND

    # 使用工厂函数构造 TestClient
    return _make_client(session_maker, authorize_scm_fn=fake_authorize_scm)


@pytest.fixture
def client_with_role(session_maker):
    """工厂 fixture：接受 ScmRole，返回注入了对应 fake authorize_scm 的 client。

    用法：
        c = client_with_role(ScmRole.CAN_QUERY)
        r = c.post(...)

    Args:
        session_maker: 由 pytest 自动注入（session_maker fixture）
    Returns:
        一个接受 ScmRole 参数的函数，返回配置好的 TestClient
    """
    # 返回一个工厂函数，而不是直接返回 client
    # 这样每次调用时可以指定不同的 role
    def factory(role: ScmRole):
        """创建注入了指定 role 的 TestClient。"""
        # async 函数：fake authorize_scm 返回调用方指定的 role
        async def fake_authorize(db, *, user, conn, repo_full_name, repo_external_id, need_bind):
            """Fake：返回调用方通过闭包传入的 role（Python 闭包：内层函数访问外层变量 role）。"""
            return role  # 通过闭包捕获外层 `role` 变量

        return _make_client(session_maker, authorize_scm_fn=fake_authorize)

    # 返回工厂函数本身，测试函数接收后再调用
    return factory


# ─── 辅助函数 ─────────────────────────────────────────────────────────────────

def _login(client: TestClient, username: str = "alice", password: str = "12345678") -> str:
    """登录指定用户，返回 JWT access_token 字符串。

    Args:
        client: TestClient 实例
        username: 用户名（默认 alice）
        password: 密码（默认 12345678）
    Returns:
        JWT access_token 字符串
    """
    # client.post：发 POST 请求，json= 参数自动序列化为 JSON body + Content-Type header
    r = client.post("/auth/login", json={
        "username": username,
        "password": password,
        "remember_me": False,  # 不记住登录状态
    })
    # f-string：Python 3.6+ 的格式化字符串，{表达式} 在运行时求值
    assert r.status_code == 200, f"登录失败：{r.text}"
    # r.json()：把响应体解析为 Python dict；["access_token"] 取 JWT 字符串
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    """构造 Bearer Authorization header 字典。

    Args:
        token: JWT access_token 字符串
    Returns:
        {"Authorization": "Bearer <token>"} 字典，传给 TestClient 的 headers 参数
    """
    # f-string 拼接 Bearer token；HTTP 标准格式
    return {"Authorization": f"Bearer {token}"}


# 可复用的合法请求体（project_id="newp"，不与 seed 数据冲突）
_BODY = {
    "project_id": "newp",           # 工程业务 ID（字符串，业务可读）
    "name": "新工程",                 # 工程显示名
    "repo_external_id": 123456,      # GitHub 仓库数字 ID（BigInteger）
    "repo_full_name": "alice/newp",  # "owner/repo" 格式
    "ref": "main",                   # 目标分支
    "ref_type": "branch",            # ref 类型
    "subpath": None,                 # 子路径（None = 全仓）
}

# 端点 URL 模板，供各测试用例使用
_ENDPOINT = "/scm/connections/{connection_id}/projects"


# ─── Task 1：连接不存在 / 他人连接 → 404 ─────────────────────────────────────

def test_create_project_unknown_connection_404(client):
    """connection_id 不存在 → 404。

    端点实现必须先查 DB，连接不存在统一返回 404（不泄露 "连接不存在" 以外的信息）。
    """
    # _login 返回 alice 的 JWT；_auth 把它包成 Authorization header
    tok = _login(client, "alice")
    # 使用不存在的 connection_id="nope"
    url = _ENDPOINT.format(connection_id="nope")
    # client.post：发 HTTP POST，headers 传 Authorization，json 传请求体
    r = client.post(url, json=_BODY, headers=_auth(tok))
    # 断言返回 404（不是 200/403/500 等）
    assert r.status_code == 404, f"期望 404，实际: {r.status_code} {r.text}"


def test_create_project_connection_not_owned_404(client):
    """alice 访问 bob 的连接 c2 → 404（归属保护，不泄露他人连接存在性）。

    端点应统一返回 404，而不是 403，避免信息泄露（攻击者无法推断哪些连接 ID 存在）。
    """
    tok = _login(client, "alice")
    # c2 属于 bob，alice 不能用
    url = _ENDPOINT.format(connection_id="c2")
    r = client.post(url, json=_BODY, headers=_auth(tok))
    assert r.status_code == 404, f"期望 404，实际: {r.status_code} {r.text}"


# ─── 零写入断言：404 路径不应有任何 DB 写入 ──────────────────────────────────

def test_no_write_on_unknown_connection(client, session_maker):
    """connection_id 不存在 → 404，且 DB 里无新增的 Project/UserProjectAccess/IndexJob。

    先记录各表初始数量，请求后再检查，确保不因"先写后校验"出现幽灵数据。
    """
    import asyncio  # 标准库：运行异步协程的工具（在同步测试函数里运行 async 代码）

    async def count_rows():
        """异步查询各表行数（内部函数，仅在此测试使用）。"""
        async with session_maker() as s:
            # scalars().one()：获取单个标量值（COUNT 的结果）
            p_count = (await s.execute(select(Project))).scalars().all()
            a_count = (await s.execute(select(UserProjectAccess))).scalars().all()
            j_count = (await s.execute(select(IndexJob))).scalars().all()
            # 返回各表行数（取 len，因为 all() 返回列表）
            return len(p_count), len(a_count), len(j_count)

    # asyncio.run：在同步上下文（普通测试函数）里运行异步协程
    p_before, a_before, j_before = asyncio.run(count_rows())

    # 发出注定 404 的请求
    tok = _login(client, "alice")
    client.post(_ENDPOINT.format(connection_id="nope"), json=_BODY, headers=_auth(tok))

    # 再次统计行数，确保没有变化
    p_after, a_after, j_after = asyncio.run(count_rows())
    # 三个表都不应增加行数
    assert p_after == p_before, "404 时 Project 不应有写入"
    assert a_after == a_before, "404 时 UserProjectAccess 不应有写入"
    assert j_after == j_before, "404 时 IndexJob 不应有写入"


# ─── Task 2：SCM 授权门（App 连接 + 权限不足 → 403） ─────────────────────────

def test_create_project_not_can_bind_403(client_with_role):
    """App 连接 c1 + authorize_scm 返 CAN_QUERY → 403。

    CAN_QUERY 只有只读权限（reporter），不能建工程；必须是 CAN_BIND（maintainer/admin）。
    """
    # client_with_role 是工厂 fixture，传入 CAN_QUERY 表示 fake 返回低权限
    c = client_with_role(ScmRole.CAN_QUERY)
    tok = _login(c, "alice")
    url = _ENDPOINT.format(connection_id="c1")
    r = c.post(url, json=_BODY, headers=_auth(tok))
    # 期望 403：有连接归属（alice 的 c1），但 SCM 权限不足
    assert r.status_code == 403, f"期望 403，实际: {r.status_code} {r.text}"


def test_no_write_on_insufficient_scm_role(client_with_role, session_maker):
    """App 连接 + CAN_QUERY → 403，且 DB 零写入。"""
    import asyncio  # 同步测试里运行 async 查询

    async def count_rows():
        """查各表当前行数。"""
        async with session_maker() as s:
            p = (await s.execute(select(Project))).scalars().all()
            a = (await s.execute(select(UserProjectAccess))).scalars().all()
            j = (await s.execute(select(IndexJob))).scalars().all()
            return len(p), len(a), len(j)

    p_before, a_before, j_before = asyncio.run(count_rows())

    # 注入 CAN_QUERY 的 fake client
    c = client_with_role(ScmRole.CAN_QUERY)
    tok = _login(c, "alice")
    c.post(_ENDPOINT.format(connection_id="c1"), json=_BODY, headers=_auth(tok))

    p_after, a_after, j_after = asyncio.run(count_rows())
    assert p_after == p_before, "403 时 Project 不应有写入"
    assert a_after == a_before, "403 时 UserProjectAccess 不应有写入"
    assert j_after == j_before, "403 时 IndexJob 不应有写入"


# ─── Task 3：PAT 连接跳过 SCM 门 ──────────────────────────────────────────────

def test_create_project_pat_skips_scm_gate(client_with_role):
    """PAT 连接（auth_type="pat"）跳过 SCM 门，即使 authorize_scm 设为 deny 也不报 403。

    PAT 连接的鉴权语义：连接归属本身就是充分条件（alice 的 cpat = alice 有 PAT 权限），
    不需要再调 authorize_scm 做 GitHub API 查权。
    预期：状态码不是 403（可能是 200 成功，或 409 因为反复执行冲突，但绝不是 403）。
    """
    # 注入一个"拒绝一切"的 fake authorize_scm
    # 如果 PAT 路径错误地调用了它，会返回 NOT_VISIBLE → 端点应报 403
    # 但我们期望 PAT 路径不调用 authorize_scm，所以最终不是 403
    c = client_with_role(ScmRole.NOT_VISIBLE)  # NOT_VISIBLE = 最低权限，拒绝绑定
    tok = _login(c, "alice")
    url = _ENDPOINT.format(connection_id="cpat")  # cpat = alice 的 PAT 连接
    r = c.post(url, json=_BODY, headers=_auth(tok))
    # 核心断言：PAT 跳过 SCM 门，状态码一定不是 403
    assert r.status_code != 403, f"PAT 连接不应经过 SCM 门，实际: {r.status_code} {r.text}"


# ─── Task 4：project_id 冲突 → 409 ────────────────────────────────────────────

def test_create_project_id_collision_409(client):
    """project_id="taken" 已在 seed 数据中存在 → 409 Conflict。

    端点应在写入前做 project_id 冲突检查，返回 409（不覆盖既有工程）。
    """
    tok = _login(client, "alice")
    url = _ENDPOINT.format(connection_id="c1")
    # 构造 project_id="taken" 的请求体（override _BODY 的 project_id 字段）
    # dict(**_BODY, project_id="taken")：展开 _BODY 并覆写 project_id 键
    body = {**_BODY, "project_id": "taken"}
    r = client.post(url, json=body, headers=_auth(tok))
    # 期望 409 Conflict
    assert r.status_code == 409, f"期望 409，实际: {r.status_code} {r.text}"


def test_no_write_on_collision(client, session_maker):
    """project_id 冲突 → 409，且 DB 零写入（UserProjectAccess/IndexJob 不新增）。"""
    import asyncio  # 同步测试里运行 async 查询

    async def count_rows():
        """查各表当前行数。"""
        async with session_maker() as s:
            p = (await s.execute(select(Project))).scalars().all()
            a = (await s.execute(select(UserProjectAccess))).scalars().all()
            j = (await s.execute(select(IndexJob))).scalars().all()
            return len(p), len(a), len(j)

    p_before, a_before, j_before = asyncio.run(count_rows())

    tok = _login(client, "alice")
    body = {**_BODY, "project_id": "taken"}
    client.post(_ENDPOINT.format(connection_id="c1"), json=body, headers=_auth(tok))

    p_after, a_after, j_after = asyncio.run(count_rows())
    # Project 表可能有 "taken" 已存在，不新增
    assert p_after == p_before, "409 时 Project 不应有写入"
    assert a_after == a_before, "409 时 UserProjectAccess 不应有写入"
    assert j_after == j_before, "409 时 IndexJob 不应有写入"


# ─── Task 5：Happy Path 全流程 ────────────────────────────────────────────────

def test_create_project_happy_path(client, session_maker):
    """正常建工程：200 + DB 里建好 Project / UserProjectAccess(owner) / IndexJob(full_index)。

    黄金路径：alice 用自己的 c1 连接，project_id="newp" 不冲突，authorize_scm 返 CAN_BIND。
    断言：
      1. HTTP 200 + 响应体含 project_id + job_id
      2. DB：Project(id="newp") 存在，status="indexing"，scm_connection_id="c1"
      3. DB：UserProjectAccess(project_id="newp", role="owner") 存在，user_id = alice.id
      4. DB：IndexJob(project_id="newp", type="full_index", trigger="manual") 存在
      5. 响应 job_id 与 DB 里的 IndexJob.id 一致
    """
    import asyncio  # 同步测试里运行 async 查询

    # ── 先取得 alice.id（用于校验 UserProjectAccess.user_id） ──
    async def get_alice_id():
        """查 alice 的 user.id（int）。"""
        async with session_maker() as s:
            # scalars().first()：取第一条 ORM 对象；filter by username
            from sqlalchemy import select
            u = (await s.execute(
                select(User).where(User.username == "alice")
            )).scalars().first()
            # u.id 是 int（自增主键）
            return u.id

    alice_id = asyncio.run(get_alice_id())

    # ── 发请求 ──
    tok = _login(client, "alice")
    url = _ENDPOINT.format(connection_id="c1")
    r = client.post(url, json=_BODY, headers=_auth(tok))

    # ── 断言 1：HTTP 200 + 响应体结构 ──
    assert r.status_code == 200, f"期望 200，实际: {r.status_code} {r.text}"
    data = r.json()  # 解析响应 JSON → Python dict
    # 响应体必须含 project_id 和 job_id 字段
    assert "project_id" in data, f"响应体缺 project_id：{data}"
    assert "job_id" in data, f"响应体缺 job_id：{data}"
    # 响应的 project_id 应与请求的一致
    assert data["project_id"] == "newp", f"project_id 不对：{data['project_id']}"
    # job_id 是字符串（如 "job-xxxxxxxxxxxxxxxx"）
    assert isinstance(data["job_id"], str), f"job_id 应为字符串：{data['job_id']}"

    # ── 断言 2/3/4/5：DB 状态 ──
    async def check_db():
        """查 DB 验证写入结果。"""
        async with session_maker() as s:
            # 检查 Project
            p = await s.get(Project, "newp")
            # p 必须存在
            assert p is not None, "Project 'newp' 未写入 DB"
            # status 应该是 indexing
            assert p.status == "indexing", f"Project.status 应为 indexing，实际：{p.status}"
            # scm_connection_id 应绑定到 c1
            assert p.scm_connection_id == "c1", f"Project.scm_connection_id 应为 c1，实际：{p.scm_connection_id}"
            # repo_full_name 应与请求体一致
            assert p.repo_full_name == "alice/newp", f"Project.repo_full_name 不对：{p.repo_full_name}"
            # created_by 应是 username（"alice"），而不是 user.id 数字字符串
            assert p.created_by == "alice", f"Project.created_by 应为 username 'alice'，实际：{p.created_by}"

            # 检查 UserProjectAccess（alice 应有 owner 角色）
            # 组合主键查询：get(Model, (pk1, pk2))
            upa = await s.get(UserProjectAccess, (alice_id, "newp"))
            assert upa is not None, f"UserProjectAccess(user_id={alice_id}, project_id='newp') 未写入 DB"
            # role 必须是 owner（不是 reporter/maintainer）
            assert upa.role == "owner", f"UserProjectAccess.role 应为 owner，实际：{upa.role}"

            # 检查 IndexJob
            job = (await s.execute(
                # where 过滤：project_id="newp"
                select(IndexJob).where(IndexJob.project_id == "newp")
            )).scalars().first()
            assert job is not None, "IndexJob for 'newp' 未写入 DB"
            # type 应为 full_index（首次全量索引）
            assert job.type == "full_index", f"IndexJob.type 应为 full_index，实际：{job.type}"
            # trigger 应为 manual
            assert job.trigger == "manual", f"IndexJob.trigger 应为 manual，实际：{job.trigger}"
            # 响应的 job_id 必须与 DB 里的 job.id 一致
            assert job.id == data["job_id"], f"响应 job_id({data['job_id']}) ≠ DB job.id({job.id})"

    # asyncio.run：在同步测试函数里运行 async 查询协程
    asyncio.run(check_db())


# ─── Task 6：非法 project_id / 空 name → 422（Pydantic 输入校验） ──────────────

def test_create_project_empty_project_id_422(client):
    """project_id="" 空串 → 422。

    CreateProjectBindRequest.project_id 有 pattern 校验（slug 格式），
    空串不匹配 ^[a-z][a-z0-9-]{0,62}[a-z0-9]$ → Pydantic 在路由前拦下，返回 422。
    """
    tok = _login(client, "alice")
    url = _ENDPOINT.format(connection_id="c1")
    # 用空字符串覆盖 project_id
    body = {**_BODY, "project_id": ""}
    r = client.post(url, json=body, headers=_auth(tok))
    # Pydantic ValidationError → FastAPI 转 422 Unprocessable Entity
    assert r.status_code == 422, f"空 project_id 应报 422，实际: {r.status_code} {r.text}"


def test_create_project_invalid_project_id_422(client):
    """project_id 含大写字母 / 超长 → 422。

    'X' * 70 = 70 字符，超过 slug 最大 64 字符且含大写，不满足 pattern → 422。
    """
    tok = _login(client, "alice")
    url = _ENDPOINT.format(connection_id="c1")
    # 70 个大写 X：含大写字母 + 超长，双重不合规
    body = {**_BODY, "project_id": "X" * 70}
    r = client.post(url, json=body, headers=_auth(tok))
    assert r.status_code == 422, f"非法 project_id 应报 422，实际: {r.status_code} {r.text}"


def test_create_project_empty_name_422(client):
    """name="" 空串 → 422。

    CreateProjectBindRequest.name 有 min_length=1 校验，空串直接 422。
    """
    tok = _login(client, "alice")
    url = _ENDPOINT.format(connection_id="c1")
    # 用空字符串覆盖 name
    body = {**_BODY, "name": ""}
    r = client.post(url, json=body, headers=_auth(tok))
    assert r.status_code == 422, f"空 name 应报 422，实际: {r.status_code} {r.text}"


# ─── Task 7：commit/enqueue 失败回滚（三表零残留） ───────────────────────────────

def test_create_project_enqueue_failure_rollback(session_maker, monkeypatch):
    """enqueue_index_job 抛异常 → 事务回滚，三表零新增。

    monkeypatch 把 scm_binding_router 模块里引用的 enqueue_index_job 替换成
    始终抛 RuntimeError 的版本，模拟队列写入失败。
    断言：
      1. 端点返回 5xx（DB 异常未被吞）
      2. Project / UserProjectAccess / IndexJob 均无新增行（db 事务已回滚）
    """
    import asyncio  # 同步测试函数里运行 async 查询

    # monkeypatch 是 pytest 内置 fixture，可在测试结束后自动还原所有 patch
    # 把 scm_binding_router 模块里对 enqueue_index_job 的引用换成会抛错的版本
    # 注意：patch 目标是引用处（scm_binding_router 模块），而非定义处（indexing.queue）
    import src.service.scm_binding_router as sbr   # 导入被测模块，方便用 monkeypatch.setattr

    async def boom(*args, **kwargs):
        """替代 enqueue_index_job 的 fake：始终抛 RuntimeError，模拟队列写入失败。

        *args/**kwargs：接受任意参数（与真实签名解耦），避免因签名不匹配报错。
        """
        raise RuntimeError("fake enqueue failure")

    # monkeypatch.setattr(模块, 属性名, 新值)：把模块里的 enqueue_index_job 替换掉
    monkeypatch.setattr(sbr, "enqueue_index_job", boom)

    # ── 构造注入了 monkeypatched 路由的 mini-app ──
    # monkeypatch 已在 session_maker fixture 里设好 KE_JWT_SECRET/KE_COOKIE_SECURE
    monkeypatch.setenv("KE_JWT_SECRET", "x" * 32)
    monkeypatch.setenv("KE_COOKIE_SECURE", "false")

    from fastapi import FastAPI             # 构造测试用 mini-app
    from fastapi.testclient import TestClient  # 同步测试客户端
    from src.service.db import get_db         # 生产 get_db（要被 override）
    from src.service.auth_router import router as auth_router  # 提供 /auth/login
    from src.service.auth_dependencies import get_current_user  # JWT 解析依赖
    from src.service.scm.base import ScmRole  # CAN_BIND 枚举值

    app = FastAPI()
    app.include_router(auth_router)

    # authorize_scm：始终返回 CAN_BIND，让 SCM 门不拦截（聚焦测 enqueue 失败场景）
    async def fake_authorize(db, *, user, conn, repo_full_name, repo_external_id, need_bind):
        """Fake：始终 CAN_BIND，排除 SCM 门的干扰。"""
        return ScmRole.CAN_BIND

    # 注意：create_scm_binding_routes 会在模块里 import enqueue_index_job，
    # 但 monkeypatch.setattr 已替换了 sbr.enqueue_index_job，路由执行时会用 boom
    app.include_router(create_scm_binding_routes(
        get_current_user=get_current_user,
        get_db=get_db,
        authorize_scm=fake_authorize,
    ))

    # override get_db → 使用内存 SQLite session（与其他测试一致）
    async def override_db():
        """替换 get_db → 使用内存 SQLite session。"""
        async with session_maker() as s:
            yield s
            await s.commit()

    app.dependency_overrides[get_db] = override_db

    # raise_server_exceptions=False：不把 500 变成 pytest 异常，让我们直接检查状态码
    # 默认 raise_server_exceptions=True 会把 500 转成 Python 异常（测试直接报错）
    c = TestClient(app, raise_server_exceptions=False)

    # ── 记录基线行数 ──
    async def count_rows():
        """查询三表当前总行数。"""
        async with session_maker() as s:
            p = (await s.execute(select(Project))).scalars().all()
            a = (await s.execute(select(UserProjectAccess))).scalars().all()
            j = (await s.execute(select(IndexJob))).scalars().all()
            return len(p), len(a), len(j)

    p_before, a_before, j_before = asyncio.run(count_rows())

    # ── 发请求 ──
    tok = _login(c, "alice")
    url = _ENDPOINT.format(connection_id="c1")
    # project_id="rollback-test"：合法 slug，不与已有 seed 数据冲突
    body = {**_BODY, "project_id": "rollback-test"}
    r = c.post(url, json=body, headers=_auth(tok))

    # ── 断言 1：端点返回 5xx（异常未被吞）──
    assert r.status_code >= 500, f"enqueue 失败应返回 5xx，实际: {r.status_code} {r.text}"

    # ── 断言 2：三表零新增 ──
    p_after, a_after, j_after = asyncio.run(count_rows())
    assert p_after == p_before, "enqueue 失败时 Project 不应有残留写入"
    assert a_after == a_before, "enqueue 失败时 UserProjectAccess 不应有残留写入"
    assert j_after == j_before, "enqueue 失败时 IndexJob 不应有残留写入"


# ─── Task 8：caller_username=None 或 conn.created_by=NULL → 404（防 NULL==NULL 绕过）───

def test_create_project_null_username_404(session_maker, monkeypatch):
    """conn.created_by=NULL + 调用方 username 有值 → 404（NULL 不能匹配任何 username）。

    场景：seed 一条 created_by=None 的连接，alice 用自己的 username 去访问 → 不应绑过去。
    防御 NULL==NULL 绕过：若实现写成 conn.created_by == caller_username 而 Python
    None == None 为 True，则 created_by=NULL 的连接会被任意有效用户绕过归属检查。
    端点实现在 caller_username is None 前先判，也要保证 conn.created_by=None 时不放行。
    """
    import asyncio  # 同步测试里运行 async 查询

    # seed 一条 created_by=None 的连接
    monkeypatch.setenv("KE_JWT_SECRET", "x" * 32)
    monkeypatch.setenv("KE_COOKIE_SECURE", "false")

    async def seed_null_conn():
        """向内存 DB 添加 created_by=None 的连接 cnull。"""
        async with session_maker() as s:
            # ScmConnection(created_by=None)：模拟 DB 里有 created_by 为 NULL 的异常记录
            s.add(ScmConnection(id="cnull", created_by=None, provider="github", auth_type="pat"))
            await s.commit()

    asyncio.run(seed_null_conn())

    # ── 构造 mini-app ──
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.service.db import get_db
    from src.service.auth_router import router as auth_router
    from src.service.auth_dependencies import get_current_user
    from src.service.scm.base import ScmRole

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(create_scm_binding_routes(
        get_current_user=get_current_user,
        get_db=get_db,
        authorize_scm=None,  # PAT 连接不走 authorize_scm，这里 None 也可
    ))

    async def override_db():
        """替换 get_db → 使用内存 SQLite session。"""
        async with session_maker() as s:
            yield s
            await s.commit()

    app.dependency_overrides[get_db] = override_db
    c = TestClient(app)

    # alice 以自己身份访问 cnull 连接（cnull.created_by=None，alice.username="alice"）
    tok = _login(c, "alice")
    url = _ENDPOINT.format(connection_id="cnull")
    r = c.post(url, json=_BODY, headers=_auth(tok))

    # 无论 conn.created_by 是 None 还是其他人的 username，都应返回 404（归属不匹配）
    assert r.status_code == 404, (
        f"conn.created_by=NULL 时应返回 404（防止 NULL==NULL 绕过归属检查），实际: {r.status_code} {r.text}"
    )
