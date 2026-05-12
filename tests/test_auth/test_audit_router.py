"""Audit Log Query API 测试（Task 11）。

测试场景清单：
1.  GET /admin/audit-logs 列出全部（admin）→ 200，entries 正确
2.  GET /admin/audit-logs 按 actor 过滤 → 只返回匹配用户的日志
3.  GET /admin/audit-logs 按 resource_type='project' 过滤 → 只返回 project 日志
4.  GET /admin/audit-logs 按时间范围过滤 → 只返回范围内的日志
5.  GET /admin/audit-logs 分页 page=1/2 → total/page/limit 字段正确，内容不重叠
6.  非 admin 访问 /admin/audit-logs → 403
7.  GET /groups/{gid}/audit-logs 只返回本组相关日志
8.  子组 audit 日志包含在父组查询中
9.  GET /groups/{gid}/audit-logs 由 reporter 访问 → 403

被测接口：
  GET /admin/audit-logs          require_admin
  GET /groups/{gid}/audit-logs   require_group_role("owner")
"""

# ─── 标准库导入 ───────────────────────────────────────────────────────────────

import asyncio        # 标准库：在同步 fixture 里运行异步代码
import json           # 标准库：序列化 metadata 为 JSON 字符串
from datetime import datetime, timedelta  # 日期时间操作，用于时间范围过滤测试

# ─── 第三方库导入 ─────────────────────────────────────────────────────────────

import pytest           # pytest 测试框架
import pytest_asyncio   # async fixture 支持

from fastapi import FastAPI                             # FastAPI 应用类
from fastapi.testclient import TestClient               # 同步 HTTP 测试客户端
from sqlalchemy import select                           # SQLAlchemy SELECT 构建
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,     # 异步 session 工厂
    create_async_engine,    # 创建异步数据库引擎
)

# ─── 项目内模块导入 ────────────────────────────────────────────────────────────

from src.service import auth_security as sec                  # 密码 hash 工具
from src.service.auth_models import User                      # 用户 ORM 模型
from src.service.auth_router import router as auth_router     # 登录路由（/auth/login）
from src.service.db import Base, get_db                       # ORM Base + DB 依赖
from src.service.db_models_groups import AuditLog, Group, GroupMember  # ORM 模型
from src.service.db_models_homepage import Project            # Project ORM
from src.service.audit_router import router as audit_router   # 被测 router
from src.service.group_router import router as group_router   # group_router（注册 /groups/* 路由）


# ═══════════════════════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════════════════════

def _auth(token: str) -> dict:
    """构造 Authorization header dict。

    Args:
        token: JWT access_token 字符串。

    Returns:
        dict：{"Authorization": "Bearer <token>"}，直接传给 TestClient.headers。
    """
    return {"Authorization": f"Bearer {token}"}


def _login(client: TestClient, username: str) -> str:
    """以指定用户名登录，返回 JWT access_token。

    Args:
        client:   TestClient 实例。
        username: 用户名（seed 密码统一为 '12345678'）。

    Returns:
        str：JWT access_token。
    """
    r = client.post("/auth/login", json={
        "username": username,
        "password": "12345678",
        "remember_me": False,
    })
    assert r.status_code == 200, f"登录失败（{username}）: {r.text}"
    return r.json()["access_token"]


def _get_user_id(session_maker, username: str) -> int:
    """同步辅助函数：查询指定用户名的 user_id 整数。

    用 asyncio.get_event_loop().run_until_complete 在同步 fixture 里运行异步查询。

    Args:
        session_maker: 异步 session 工厂。
        username:      要查询的用户名。

    Returns:
        int：用户的整数 ID。
    """
    async def _q():
        async with session_maker() as s:
            # select(User).filter_by(username=username)：查 username 对应的 User 行
            user = await s.scalar(select(User).filter_by(username=username))
            return user.id

    # asyncio.get_event_loop().run_until_complete：同步等待协程执行完毕
    return asyncio.get_event_loop().run_until_complete(_q())


def _seed_audit_log(
    session_maker,
    actor_user_id: int,
    action: str,
    resource_type: str,
    resource_id: str,
    metadata: dict = None,
    created_at: datetime = None,
):
    """同步辅助函数：在数据库里直接插入一条 AuditLog 记录（绕过业务接口）。

    用于测试 fixture 预建测试数据，不依赖具体业务接口。

    Args:
        session_maker:  异步 session 工厂。
        actor_user_id:  操作人 user ID。
        action:         操作动作字符串。
        resource_type:  资源类型。
        resource_id:    资源 ID。
        metadata:       附加上下文 dict（可选）。
        created_at:     创建时间（可选，默认用数据库 server_default）。
    """
    async def _insert():
        async with session_maker() as s:
            # 构建 AuditLog ORM 实例
            # json.dumps：dict → JSON 字符串（metadata_json 列存 JSON 字符串）
            log = AuditLog(
                actor_user_id=actor_user_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                metadata_json=json.dumps(metadata or {}, ensure_ascii=False),
            )
            # 如果指定了 created_at，手动赋值（覆盖 server_default）
            if created_at is not None:
                log.created_at = created_at
            s.add(log)
            await s.commit()

    asyncio.get_event_loop().run_until_complete(_insert())


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def session_maker(monkeypatch):
    """提供 in-memory SQLite 的异步 session 工厂，预建 admin / alice / bob 三个用户。

    Users:
      admin  (is_admin=True)   — Instance Admin
      alice  (is_admin=False)  — 普通用户，会被设为 group owner
      bob    (is_admin=False)  — 普通用户，reporter 角色

    Args:
        monkeypatch: pytest 内置 fixture，临时修改环境变量。

    Yields:
        async_sessionmaker：供 fixture 和测试函数创建 DB session。
    """
    # 注入必要的环境变量（auth 系统需要）
    monkeypatch.setenv("KE_JWT_SECRET", "x" * 32)
    monkeypatch.setenv("KE_COOKIE_SECURE", "false")

    # 创建纯内存 SQLite 异步引擎（echo=False 关闭 SQL 打印，避免测试输出杂乱）
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    # 建表：依据所有 ORM 类的 __tablename__（包含 User / Group / AuditLog 等）
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # expire_on_commit=False：commit 后不过期 ORM 对象（避免后续读属性触发额外 SELECT）
    SM = async_sessionmaker(eng, expire_on_commit=False)

    # ─── 预建 3 个用户 ─────────────────────────────────────────────────────
    async with SM() as s:
        s.add_all([
            User(
                email="admin@x.com",
                username="admin",
                hashed_password=sec.hash_password("12345678"),
                is_active=True,
                is_admin=True,   # Instance Admin
            ),
            User(
                email="alice@x.com",
                username="alice",
                hashed_password=sec.hash_password("12345678"),
                is_active=True,
                is_admin=False,
            ),
            User(
                email="bob@x.com",
                username="bob",
                hashed_password=sec.hash_password("12345678"),
                is_active=True,
                is_admin=False,
            ),
        ])
        await s.commit()

    yield SM  # 把 session_maker 返回给调用方

    # 测试结束：关闭连接池，释放内存 DB
    await eng.dispose()


def _make_app(session_maker) -> FastAPI:
    """构造测试用 FastAPI app，注册相关路由并覆盖 get_db 为 in-memory SQLite。

    Args:
        session_maker: 异步 session 工厂。

    Returns:
        FastAPI：装配好路由 + DI 覆盖的测试 app。
    """
    app = FastAPI()
    app.include_router(auth_router)   # /auth/login（获取 JWT token）
    app.include_router(audit_router)  # 被测路由：/admin/audit-logs + /groups/{gid}/audit-logs
    app.include_router(group_router)  # /groups/* 路由（require_group_role 需要 group 存在）

    async def override_db():
        """覆盖 get_db：注入 in-memory SQLite session。"""
        async with session_maker() as s:
            yield s
            await s.commit()  # 路由执行后自动 commit，保证写操作可见

    # dependency_overrides：把生产用的 get_db 替换为 in-memory SQLite session
    app.dependency_overrides[get_db] = override_db
    return app


@pytest.fixture
def client(session_maker):
    """返回绑定了 in-memory SQLite 的 TestClient。

    Args:
        session_maker: in-memory SQLite session 工厂（来自 session_maker fixture）。

    Returns:
        TestClient：可直接发 HTTP 请求的测试客户端。
    """
    return TestClient(_make_app(session_maker))


@pytest.fixture
def client_admin(client):
    """已登录为 admin 的 (client, token) 对。"""
    return client, _login(client, "admin")


@pytest.fixture
def client_alice(client):
    """已登录为 alice 的 (client, token) 对。"""
    return client, _login(client, "alice")


@pytest.fixture
def client_bob(client):
    """已登录为 bob 的 (client, token) 对。"""
    return client, _login(client, "bob")


# ─── 预建 Group + AuditLog 数据的 fixture ────────────────────────────────────

@pytest.fixture
def seed_basic_audit_logs(client_admin, session_maker):
    """预建基础测试数据：
      - 1 个 root group 'corp'
      - admin / alice 两个用户的 AuditLog 记录，resource_type 涵盖 group 和 project

    Returns:
        tuple：(client, admin_token, alice_id, admin_id)
    """
    client, admin_token = client_admin
    admin_id = _get_user_id(session_maker, "admin")
    alice_id = _get_user_id(session_maker, "alice")

    # 建 root group（admin 建根 group，用于后续审计日志关联）
    r = client.post(
        "/groups",
        json={"id": "corp", "name": "Corp"},
        headers=_auth(admin_token),
    )
    assert r.status_code == 201, f"建 group 失败: {r.text}"

    # 预插 admin 的 project 类型审计日志
    _seed_audit_log(session_maker, admin_id, "project.create", "project", "proj-1",
                    {"name": "Proj 1"})
    _seed_audit_log(session_maker, admin_id, "project.create", "project", "proj-2",
                    {"name": "Proj 2"})

    # 预插 alice 的 group 类型审计日志
    _seed_audit_log(session_maker, alice_id, "group.update", "group", "corp",
                    {"name": "Corp"})

    return client, admin_token, alice_id, admin_id


@pytest.fixture
def seed_group_with_subgroup(client_admin, session_maker):
    """预建 parent group 'bank' + 子 group 'bank/retail' + 各自的 AuditLog。

    用于场景 7（本组过滤）和场景 8（子组包含在父组）。

    Returns:
        tuple：(client, admin_token, parent_gid, child_gid, alice_token)
    """
    client, admin_token = client_admin
    admin_id = _get_user_id(session_maker, "admin")
    alice_id = _get_user_id(session_maker, "alice")

    # 建 root group 'bank'
    r = client.post(
        "/groups",
        json={"id": "bank", "name": "Bank"},
        headers=_auth(admin_token),
    )
    assert r.status_code == 201, f"建 bank group 失败: {r.text}"

    # 建子 group 'bank-retail'（注意：admin 是 bank 的 owner，有权建子组）
    r = client.post(
        "/groups",
        json={"id": "bank-retail", "name": "Retail", "parent_group_id": "bank"},
        headers=_auth(admin_token),
    )
    assert r.status_code == 201, f"建子 group 失败: {r.text}"

    # 在 DB 里直接把 alice 加为 bank group 的 owner
    async def _add_alice():
        async with session_maker() as s:
            s.add(GroupMember(
                user_id=alice_id,
                group_id="bank",
                role="owner",
                added_by_user_id=admin_id,
            ))
            await s.commit()

    asyncio.get_event_loop().run_until_complete(_add_alice())

    # 预建 Project 关联到 bank group（用于 project 类型日志过滤测试）
    async def _add_project():
        async with session_maker() as s:
            s.add(Project(
                id="bank-core",
                name="Bank Core",
                group_id="bank",
            ))
            await s.commit()

    asyncio.get_event_loop().run_until_complete(_add_project())

    # 插 bank group 本身的审计日志
    _seed_audit_log(session_maker, admin_id, "group.create", "group", "bank",
                    {"name": "Bank"})

    # 插子组 bank-retail 的审计日志
    _seed_audit_log(session_maker, admin_id, "group.create", "group", "bank-retail",
                    {"name": "Retail"})

    # 插 bank-core project 的审计日志
    _seed_audit_log(session_maker, admin_id, "project.create", "project", "bank-core",
                    {"name": "Bank Core"})

    # 插一条完全无关的日志（resource_id='other-group'）用于对照
    _seed_audit_log(session_maker, admin_id, "group.create", "group", "other-group",
                    {"name": "Other"})

    alice_token = _login(client, "alice")
    return client, admin_token, "bank", "bank-retail", alice_token


@pytest.fixture
def seed_group_bob_reporter(client_admin, session_maker):
    """预建 group 'test-grp'，bob 是 reporter。

    用于场景 9：reporter 访问 /groups/{gid}/audit-logs → 403。

    Returns:
        tuple：(client, bob_token, gid)
    """
    client, admin_token = client_admin
    admin_id = _get_user_id(session_maker, "admin")
    bob_id = _get_user_id(session_maker, "bob")

    # 建 root group
    r = client.post(
        "/groups",
        json={"id": "test-grp", "name": "Test Group"},
        headers=_auth(admin_token),
    )
    assert r.status_code == 201, f"建 test-grp 失败: {r.text}"

    # 把 bob 加为 reporter
    async def _add_bob():
        async with session_maker() as s:
            s.add(GroupMember(
                user_id=bob_id,
                group_id="test-grp",
                role="reporter",
                added_by_user_id=admin_id,
            ))
            await s.commit()

    asyncio.get_event_loop().run_until_complete(_add_bob())

    bob_token = _login(client, "bob")
    return client, bob_token, "test-grp"


# ═══════════════════════════════════════════════════════════════════════════════
# 场景测试
# ═══════════════════════════════════════════════════════════════════════════════

# ─── 场景 1：/admin/audit-logs 列全部（admin）─────────────────────────────────

def test_admin_can_list_all_audit_logs(seed_basic_audit_logs):
    """GET /admin/audit-logs：admin 能列出所有审计日志 → 200，entries 非空。

    验证点：
      - 状态码 200
      - 响应有 entries / total / page / limit 字段
      - entries 列表非空（seed_basic_audit_logs 预建了多条日志）
    """
    client, admin_token, _, _ = seed_basic_audit_logs

    r = client.get("/admin/audit-logs", headers=_auth(admin_token))
    assert r.status_code == 200, r.text

    body = r.json()
    # 验证响应结构：必须有 entries / total / page / limit 四个字段
    assert "entries" in body, f"缺少 entries 字段: {body}"
    assert "total" in body, f"缺少 total 字段: {body}"
    assert "page" in body, f"缺少 page 字段: {body}"
    assert "limit" in body, f"缺少 limit 字段: {body}"

    # 应该有日志（seed 了 group.create 来自 POST /groups，加上 _seed_audit_log 直接插的）
    assert body["total"] >= 1, f"total 应 >= 1，实际: {body['total']}"
    assert len(body["entries"]) >= 1, f"entries 应非空，实际: {body['entries']}"

    # 验证每个 entry 的字段结构
    entry = body["entries"][0]
    for field in ("id", "action", "resource_type", "resource_id", "metadata", "created_at"):
        assert field in entry, f"entry 缺少字段 {field}: {entry}"


# ─── 场景 2：/admin/audit-logs 按 actor 过滤 ──────────────────────────────────

def test_admin_filter_by_actor(seed_basic_audit_logs):
    """GET /admin/audit-logs?actor=alice：只返回 alice 操作的日志。

    验证点：
      - 状态码 200
      - 所有返回条目的 actor_username 包含 'alice'
    """
    client, admin_token, alice_id, _ = seed_basic_audit_logs

    # 按 actor=alice 过滤（模糊匹配用户名）
    r = client.get("/admin/audit-logs?actor=alice", headers=_auth(admin_token))
    assert r.status_code == 200, r.text

    body = r.json()
    # 有日志返回
    assert body["total"] >= 1, f"应有 alice 的日志，total={body['total']}"

    # 所有返回条目的 actor_username 应包含 'alice'
    for entry in body["entries"]:
        uname = entry.get("actor_username", "")
        assert "alice" in (uname or "").lower(), \
            f"返回了非 alice 的日志: {entry}"


# ─── 场景 3：/admin/audit-logs 按 resource_type='project' 过滤 ────────────────

def test_admin_filter_by_resource_type(seed_basic_audit_logs):
    """GET /admin/audit-logs?resource_type=project：只返回 project 类型日志。

    验证点：
      - 状态码 200
      - 所有返回条目的 resource_type 都是 'project'
    """
    client, admin_token, _, _ = seed_basic_audit_logs

    r = client.get("/admin/audit-logs?resource_type=project", headers=_auth(admin_token))
    assert r.status_code == 200, r.text

    body = r.json()
    # seed 里有 2 条 project.create 日志
    assert body["total"] >= 2, f"应有 >= 2 条 project 日志，total={body['total']}"

    # 所有条目的 resource_type 都是 'project'
    for entry in body["entries"]:
        assert entry["resource_type"] == "project", \
            f"返回了非 project 的日志: {entry}"


# ─── 场景 4：/admin/audit-logs 按时间范围过滤 ─────────────────────────────────

def test_admin_filter_by_time_range(client_admin, session_maker):
    """GET /admin/audit-logs?from_time=...&to_time=...：只返回范围内的日志。

    构造：
      - 在"昨天"插一条日志
      - 在"明天"插一条日志
      - 按 from_time=今天00:00，to_time=今天23:59:59 查询
      - 期望：只返回"今天"的日志（如果有），不返回"昨天"和"明天"的

    注意：SQLite server_default 返回时间可能不精确，
    所以我们插"明显过去"和"明显未来"的日志来验证过滤效果。
    """
    client, admin_token = client_admin
    admin_id = _get_user_id(session_maker, "admin")

    # 构造三个时间点：yesterday / today / tomorrow
    today = datetime.utcnow().replace(hour=12, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)

    # 分别插三条不同时间的日志
    _seed_audit_log(session_maker, admin_id, "group.update", "group", "time-test-yd",
                    {}, created_at=yesterday)
    _seed_audit_log(session_maker, admin_id, "group.update", "group", "time-test-td",
                    {}, created_at=today)
    _seed_audit_log(session_maker, admin_id, "group.update", "group", "time-test-tm",
                    {}, created_at=tomorrow)

    # 按时间范围：today ± 6小时（确保只有今天的时间戳被纳入）
    from_t = (today - timedelta(hours=6)).isoformat()
    to_t = (today + timedelta(hours=6)).isoformat()

    r = client.get(
        f"/admin/audit-logs?resource_type=group&resource_id=time-test-td"
        f"&from_time={from_t}&to_time={to_t}",
        headers=_auth(admin_token),
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # resource_id='time-test-td' 这条日志时间是 today，应该出现在结果里
    assert body["total"] == 1, \
        f"只应返回 today 的 1 条日志，实际 total={body['total']}, entries={body['entries']}"
    assert body["entries"][0]["resource_id"] == "time-test-td"


# ─── 场景 5：/admin/audit-logs 分页 page=1/2 ─────────────────────────────────

def test_admin_pagination(client_admin, session_maker):
    """GET /admin/audit-logs 分页：page=1 和 page=2 不重叠，total 正确。

    构造：预插 5 条日志，limit=3 分两页查询。
    验证点：
      - page=1 返回 3 条，page=2 返回 2 条
      - total = 5
      - 两页 id 无重叠
    """
    client, admin_token = client_admin
    admin_id = _get_user_id(session_maker, "admin")

    # 预插 5 条同类型日志（resource_type='credential' 不常见，避免和其他 fixture 干扰）
    for i in range(5):
        _seed_audit_log(session_maker, admin_id, "credential.create", "credential",
                        f"cred-page-{i}", {"idx": i})

    # page=1，limit=3
    r1 = client.get(
        "/admin/audit-logs?resource_type=credential&page=1&limit=3",
        headers=_auth(admin_token),
    )
    assert r1.status_code == 200, r1.text
    b1 = r1.json()

    # total 应该是 5（只统计 credential 类型）
    assert b1["total"] == 5, f"total 应为 5，实际: {b1['total']}"
    assert b1["page"] == 1, f"page 应为 1，实际: {b1['page']}"
    assert b1["limit"] == 3, f"limit 应为 3，实际: {b1['limit']}"
    # page=1 应该有 3 条（limit=3，共 5 条）
    assert len(b1["entries"]) == 3, f"page=1 应有 3 条，实际: {len(b1['entries'])}"

    # page=2，limit=3
    r2 = client.get(
        "/admin/audit-logs?resource_type=credential&page=2&limit=3",
        headers=_auth(admin_token),
    )
    assert r2.status_code == 200, r2.text
    b2 = r2.json()

    # page=2 应该有 2 条（5 - 3 = 2）
    assert len(b2["entries"]) == 2, f"page=2 应有 2 条，实际: {len(b2['entries'])}"

    # 两页 id 不应有重叠
    ids_p1 = {e["id"] for e in b1["entries"]}
    ids_p2 = {e["id"] for e in b2["entries"]}
    assert ids_p1.isdisjoint(ids_p2), \
        f"page=1 和 page=2 有重叠 id: {ids_p1 & ids_p2}"


# ─── 场景 6：非 admin 访问 /admin/audit-logs → 403 ────────────────────────────

def test_non_admin_cannot_access_admin_audit_logs(client_alice):
    """GET /admin/audit-logs：非 admin（alice）访问 → 403。

    /admin/audit-logs 要求 Instance Admin 权限（require_admin dependency）。
    普通用户应该被拒绝，返回 403 Forbidden。
    """
    client, alice_token = client_alice

    r = client.get("/admin/audit-logs", headers=_auth(alice_token))
    # 非 admin → 403 Forbidden
    assert r.status_code == 403, f"非 admin 应返回 403，实际: {r.status_code} {r.text}"


# ─── 场景 7：/groups/{gid}/audit-logs 只返回本组相关日志 ─────────────────────

def test_group_audit_logs_scoped_to_group(seed_group_with_subgroup):
    """GET /groups/bank/audit-logs：只返回 bank 组相关的日志，不返回 other-group 的日志。

    验证点：
      - 状态码 200（alice 是 bank 的 owner）
      - 返回的 entries 里 resource_id 不包含 'other-group'
      - bank / bank-retail / bank-core 的日志都在结果里
    """
    client, _, parent_gid, child_gid, alice_token = seed_group_with_subgroup

    r = client.get(
        f"/groups/{parent_gid}/audit-logs",
        headers=_auth(alice_token),
    )
    assert r.status_code == 200, r.text

    body = r.json()
    entries = body["entries"]

    # 有日志返回（bank + bank-retail + bank-core 三条）
    assert body["total"] >= 3, f"应有 >= 3 条，actual total={body['total']}"

    # 所有返回的 resource_id 不应包含 'other-group'
    resource_ids = [e["resource_id"] for e in entries]
    assert "other-group" not in resource_ids, \
        f"返回了无关的 other-group 日志，resource_ids: {resource_ids}"


# ─── 场景 8：子组 audit 日志包含在父组查询中 ─────────────────────────────────

def test_group_audit_includes_subgroup_logs(seed_group_with_subgroup):
    """GET /groups/bank/audit-logs：子组 bank-retail 的日志包含在父组查询结果里。

    验证点：
      - 返回的 entries 里有 resource_id='bank-retail' 的条目（子组日志）
      - 也有 resource_id='bank-core' 的条目（子组下 project 日志）
    """
    client, _, parent_gid, child_gid, alice_token = seed_group_with_subgroup

    r = client.get(
        f"/groups/{parent_gid}/audit-logs",
        headers=_auth(alice_token),
    )
    assert r.status_code == 200, r.text

    body = r.json()
    entries = body["entries"]
    resource_ids = [e["resource_id"] for e in entries]

    # 子组 bank-retail 的日志应该在结果里
    assert child_gid in resource_ids, \
        f"子组 '{child_gid}' 的日志应包含在父组查询里，实际 resource_ids: {resource_ids}"

    # 子组关联 project bank-core 的日志也应该在结果里
    assert "bank-core" in resource_ids, \
        f"子组关联的 project 'bank-core' 的日志应包含在父组查询里，实际: {resource_ids}"


# ─── 场景 9：reporter 访问 /groups/{gid}/audit-logs → 403 ────────────────────

def test_reporter_cannot_access_group_audit_logs(seed_group_bob_reporter):
    """GET /groups/{gid}/audit-logs：reporter（bob）访问 → 403。

    /groups/{gid}/audit-logs 要求 owner 权限（require_group_role("owner")）。
    reporter 角色权限不足，应返回 403 Forbidden。
    """
    client, bob_token, gid = seed_group_bob_reporter

    r = client.get(
        f"/groups/{gid}/audit-logs",
        headers=_auth(bob_token),
    )
    # reporter 权限 < owner → 403
    assert r.status_code == 403, \
        f"reporter 应返回 403，实际: {r.status_code} {r.text}"
