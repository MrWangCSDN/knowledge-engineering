"""User Management CRUD 路由测试（Task 10）。

测试场景清单：
1. admin 列所有用户 → 200（包含 seed 用户）
2. 非 admin 访问 /admin/users → 403
3. admin 创建新用户（is_admin=False）→ 201
4. admin 改 is_admin=True → 200，字段更新
5. admin 改 is_admin=False（普通）→ 200，字段更新
6. admin 改 is_active=False → 200，字段更新
7. admin 改密码 → 200，hashed_password 已变更（数据库层验证）
8. 降级最后一个 admin（admin 自身）→ 422
9. 删用户但有 group ownership → 422
10. 删用户但有 project ownership → 422
11. 删干净用户 → 204

被测接口：
  GET    /admin/users              Depends(get_current_admin)
  POST   /admin/users              Depends(get_current_admin)
  PATCH  /admin/users/{uid}        Depends(get_current_admin)
  DELETE /admin/users/{uid}        Depends(get_current_admin)
"""

# ─── 标准库 / 第三方库导入 ─────────────────────────────────────────────────────

import asyncio          # 标准库：在同步 fixture 里运行异步代码
import pytest           # pytest 测试框架
import pytest_asyncio   # 扩展 pytest 支持 async fixture

from fastapi import FastAPI                         # FastAPI 应用类
from fastapi.testclient import TestClient           # 同步 HTTP 测试客户端
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,     # 异步 session 工厂
    create_async_engine,    # 创建异步数据库引擎
)
from sqlalchemy import select   # 构造 SELECT 查询语句

# ─── 项目内模块导入 ──────────────────────────────────────────────────────────────

from src.service import auth_security as sec                   # 密码工具
from src.service.auth_models import User                       # 用户 ORM 模型
from src.service.auth_router import router as auth_router      # 登录路由
from src.service.db import Base, get_db                        # ORM Base + DB 依赖
from src.service.db_models_groups import Group, GroupMember    # Group / GroupMember ORM
from src.service.db_models_homepage import Project, UserProjectAccess  # Project / 成员 ORM
from src.service.user_router import router as user_router      # 被测 router


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def session_maker(monkeypatch):
    """提供 in-memory SQLite 的异步 session 工厂，预建 3 个用户。

    用户：
      admin  (is_admin=True)   — 唯一的 Instance Admin（用于测试"最后 admin 保护"）
      alice  (is_admin=False)  — 普通用户，多数场景下作为被管理的目标用户
      bob    (is_admin=False)  — 普通用户，用于测试"非 admin 访问"

    Args:
        monkeypatch: pytest 内置 fixture，临时修改环境变量。

    Yields:
        async_sessionmaker：供下游 fixture 创建 DB session。
    """
    # 注入 JWT 密钥（登录 /auth/login 时需要）
    monkeypatch.setenv("KE_JWT_SECRET", "x" * 32)
    # 关闭 Secure cookie（测试用 http，不需要 https）
    monkeypatch.setenv("KE_COOKIE_SECURE", "false")

    # 创建纯内存 SQLite 异步引擎（测试数据完全隔离，不写磁盘）
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    # 建表（依据所有 ORM 类的 __tablename__）
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # 创建异步 session 工厂
    # expire_on_commit=False：commit 后 ORM 对象属性不失效（测试更简便）
    SM = async_sessionmaker(eng, expire_on_commit=False)

    # ─── seed 3 个用户 ──────────────────────────────────────────────────────
    async with SM() as s:
        s.add_all([
            User(
                email="admin@x.com",
                username="admin",
                hashed_password=sec.hash_password("12345678"),
                is_active=True,
                is_admin=True,   # 唯一 Instance Admin
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

    yield SM  # yield：测试执行时使用 SM，测试结束后继续执行后续清理

    # ─── 测试结束清理 ──────────────────────────────────────────────────────
    # eng.dispose()：关闭连接池，释放内存数据库资源
    await eng.dispose()


def _make_app(session_maker) -> FastAPI:
    """构造测试用 FastAPI app，注册 auth_router + user_router，并覆盖 get_db。

    Args:
        session_maker: async_sessionmaker，来自 session_maker fixture。

    Returns:
        FastAPI：装配好路由 + DI 覆盖的测试 app。
    """
    app = FastAPI()
    app.include_router(auth_router)   # 登录路由（/auth/login）
    app.include_router(user_router)   # 被测路由（/admin/users/*）

    async def override_db():
        """覆盖 get_db：注入 in-memory SQLite session。

        async generator 函数：yield 前是"请求前"逻辑，yield 后是"请求后"清理。
        """
        async with session_maker() as s:
            yield s
            # 不在 override 里 commit（让路由函数自己 commit），
            # 但保留 override 结构以便 autoflush 能工作

    # dependency_overrides：FastAPI 的 DI 覆盖机制，把 get_db 替换为测试版
    app.dependency_overrides[get_db] = override_db
    return app


def _login(client: TestClient, username: str) -> str:
    """辅助：以指定用户名登录，返回 JWT access_token。

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
    assert r.status_code == 200, f"登录失败: {r.text}"
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    """辅助：构造 Authorization header dict。

    Args:
        token: JWT access_token。

    Returns:
        dict：{"Authorization": "Bearer <token>"}
    """
    return {"Authorization": f"Bearer {token}"}


# ─── client fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def client(session_maker):
    """返回绑定了 in-memory SQLite 的 TestClient。"""
    return TestClient(_make_app(session_maker))


@pytest.fixture
def client_admin(client):
    """已登录为 admin 的 (client, token) 对。"""
    token = _login(client, "admin")
    return client, token


@pytest.fixture
def client_alice(client):
    """已登录为 alice（普通用户）的 (client, token) 对。"""
    token = _login(client, "alice")
    return client, token


# ─── 辅助：查询用户 ID ────────────────────────────────────────────────────────

def _get_user_id(session_maker, username: str) -> int:
    """同步辅助：查询指定用户名的 user_id（整数）。

    在同步 fixture 里用 asyncio.get_event_loop().run_until_complete(...)
    来运行异步数据库查询。

    Args:
        session_maker: 异步 session 工厂。
        username:      要查询的用户名。

    Returns:
        int：用户的整数 ID。
    """
    async def _query():
        async with session_maker() as s:
            user = await s.scalar(select(User).filter_by(username=username))
            return user.id

    return asyncio.get_event_loop().run_until_complete(_query())


def _get_hashed_password(session_maker, uid: int) -> str:
    """同步辅助：查询指定用户的 hashed_password（用于验证密码已变更）。

    Args:
        session_maker: 异步 session 工厂。
        uid:           用户整数 ID。

    Returns:
        str：数据库里存储的 bcrypt hash。
    """
    async def _query():
        async with session_maker() as s:
            user = await s.get(User, uid)
            return user.hashed_password

    return asyncio.get_event_loop().run_until_complete(_query())


# ═══════════════════════════════════════════════════════════════════════════════
# 测试场景
# ═══════════════════════════════════════════════════════════════════════════════

# ─── 场景 1：admin 列所有用户 ────────────────────────────────────────────────

def test_list_users_as_admin(client_admin):
    """admin 列所有用户 → 200，返回至少 3 个 seed 用户。"""
    client, token = client_admin

    r = client.get("/admin/users", headers=_auth(token))

    # 断言：HTTP 200 OK
    assert r.status_code == 200, r.text

    data = r.json()
    # seed 了 3 个用户（admin / alice / bob），应该至少有 3 条记录
    assert isinstance(data, list)
    assert len(data) >= 3

    # 验证返回字段包含 id / email / username / is_active / is_admin / created_at
    first = data[0]
    assert "id" in first
    assert "email" in first
    assert "username" in first
    assert "is_active" in first
    assert "is_admin" in first
    assert "created_at" in first

    # 响应里不包含 hashed_password（安全原则）
    assert "hashed_password" not in first
    assert "password" not in first


# ─── 场景 2：非 admin 访问 /admin/users → 403 ─────────────────────────────

def test_list_users_non_admin_forbidden(client_alice):
    """非 admin 用户访问 /admin/users → 403 Forbidden。"""
    client, token = client_alice

    r = client.get("/admin/users", headers=_auth(token))

    # Instance Admin 保护：alice 是普通用户，应该被拒绝
    assert r.status_code == 403, r.text


# ─── 场景 3：admin 创建新用户（is_admin=False）─────────────────────────────

def test_create_user_by_admin(client_admin):
    """admin 创建新用户（is_admin=False）→ 201，返回正确字段。"""
    client, token = client_admin

    r = client.post(
        "/admin/users",
        json={
            "email": "charlie@x.com",
            "username": "charlie",
            "password": "abcdefgh",
            "is_admin": False,
        },
        headers=_auth(token),
    )

    # 断言：HTTP 201 Created
    assert r.status_code == 201, r.text

    data = r.json()
    assert data["username"] == "charlie"
    assert data["email"] == "charlie@x.com"
    assert data["is_admin"] is False
    assert data["is_active"] is True  # 创建时默认激活

    # 不含密码 hash
    assert "hashed_password" not in data


def test_create_user_duplicate_email(client_admin):
    """创建已存在邮箱的用户 → 409 Conflict。"""
    client, token = client_admin

    # 先创建一个用户
    client.post(
        "/admin/users",
        json={"email": "dup@x.com", "username": "dup1", "password": "12345678"},
        headers=_auth(token),
    )

    # 再用相同邮箱创建 → 409
    r = client.post(
        "/admin/users",
        json={"email": "dup@x.com", "username": "dup2", "password": "12345678"},
        headers=_auth(token),
    )
    assert r.status_code == 409, r.text


def test_create_user_duplicate_username(client_admin):
    """创建已存在用户名的用户 → 409 Conflict。"""
    client, token = client_admin

    # 先创建一个用户
    client.post(
        "/admin/users",
        json={"email": "uniq1@x.com", "username": "dupname", "password": "12345678"},
        headers=_auth(token),
    )

    # 再用相同用户名创建 → 409
    r = client.post(
        "/admin/users",
        json={"email": "uniq2@x.com", "username": "dupname", "password": "12345678"},
        headers=_auth(token),
    )
    assert r.status_code == 409, r.text


# ─── 场景 4：admin 改 is_admin=True ────────────────────────────────────────

def test_set_admin_true(client_admin, session_maker):
    """admin 把 alice 的 is_admin 改为 True → 200，字段更新。"""
    client, token = client_admin

    alice_id = _get_user_id(session_maker, "alice")

    r = client.patch(
        f"/admin/users/{alice_id}",
        json={"is_admin": True},
        headers=_auth(token),
    )

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["is_admin"] is True


# ─── 场景 5：admin 改 is_admin=False（普通用户，非最后 admin）──────────────

def test_set_admin_false_ok(client_admin, session_maker):
    """admin 先把 alice 升为 admin，再降回 False → 200 OK（此时还有 admin 本身）。"""
    client, token = client_admin

    alice_id = _get_user_id(session_maker, "alice")

    # 先升为 admin（现在有 2 个 admin：admin + alice）
    r_up = client.patch(
        f"/admin/users/{alice_id}",
        json={"is_admin": True},
        headers=_auth(token),
    )
    assert r_up.status_code == 200, r_up.text

    # 再降级 alice（还剩 admin 本身，合法）
    r_down = client.patch(
        f"/admin/users/{alice_id}",
        json={"is_admin": False},
        headers=_auth(token),
    )
    assert r_down.status_code == 200, r_down.text
    data = r_down.json()
    assert data["is_admin"] is False


# ─── 场景 6：admin 改 is_active=False ────────────────────────────────────

def test_deactivate_user(client_admin, session_maker):
    """admin 把 alice 停用（is_active=False）→ 200，字段更新。"""
    client, token = client_admin

    alice_id = _get_user_id(session_maker, "alice")

    r = client.patch(
        f"/admin/users/{alice_id}",
        json={"is_active": False},
        headers=_auth(token),
    )

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["is_active"] is False


# ─── 场景 7：admin 改密码 → hashed_password 已变更 ────────────────────────

def test_change_password(client_admin, session_maker):
    """admin 改 alice 的密码 → 200，数据库里 hashed_password 已变更。"""
    client, token = client_admin

    alice_id = _get_user_id(session_maker, "alice")

    # 记录旧 hash（从数据库直接查）
    old_hash = _get_hashed_password(session_maker, alice_id)

    r = client.patch(
        f"/admin/users/{alice_id}",
        json={"password": "newpassword123"},
        headers=_auth(token),
    )

    assert r.status_code == 200, r.text

    # 从数据库重新查新 hash，断言已变更
    new_hash = _get_hashed_password(session_maker, alice_id)
    assert new_hash != old_hash, "密码 hash 应该已经变更"

    # 验证新密码可以验证（confirm hash is valid bcrypt）
    assert sec.verify_password("newpassword123", new_hash) is True


# ─── 场景 8：降级最后一个 admin → 422 ─────────────────────────────────────

def test_downgrade_last_admin_rejected(client_admin, session_maker):
    """试图把唯一的 Instance Admin（admin 自身）降级为 False → 422。"""
    client, token = client_admin

    admin_id = _get_user_id(session_maker, "admin")

    r = client.patch(
        f"/admin/users/{admin_id}",
        json={"is_admin": False},
        headers=_auth(token),
    )

    # 不能降级最后一个 Instance Admin
    assert r.status_code == 422, r.text
    assert "Instance Admin" in r.json()["detail"]


# ─── 场景 9：删用户但有 group ownership → 422 ─────────────────────────────

def test_delete_user_with_group_ownership_rejected(client_admin, session_maker):
    """删除在某 group 中是 owner 的用户 → 422 "先转让"。"""
    client, token = client_admin

    alice_id = _get_user_id(session_maker, "alice")
    admin_id = _get_user_id(session_maker, "admin")

    # 在 DB 里给 alice 创建一个 group，并设置 alice 为 owner
    async def _seed_group():
        async with session_maker() as s:
            s.add(Group(
                id="test-group",
                name="Test Group",
                created_by_user_id=admin_id,
            ))
            # alice 是这个 group 的 owner
            s.add(GroupMember(
                user_id=alice_id,
                group_id="test-group",
                role="owner",
                added_by_user_id=admin_id,
            ))
            await s.commit()

    asyncio.get_event_loop().run_until_complete(_seed_group())

    # 尝试删除 alice（她是 group 的 owner）→ 应该被拒绝
    r = client.delete(f"/admin/users/{alice_id}", headers=_auth(token))

    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    # 错误信息应该包含"组"和"owner"相关字样
    assert "组" in detail or "owner" in detail.lower()


# ─── 场景 10：删用户但有 project ownership → 422 ──────────────────────────

def test_delete_user_with_project_ownership_rejected(client_admin, session_maker):
    """删除在某 project 中是 owner 的用户 → 422 "先转让"。"""
    client, token = client_admin

    alice_id = _get_user_id(session_maker, "alice")
    admin_id = _get_user_id(session_maker, "admin")

    # 在 DB 里创建一个 project，并设置 alice 为 owner
    async def _seed_project():
        async with session_maker() as s:
            s.add(Project(
                id="test-proj",
                name="Test Project",
                created_by="admin",
            ))
            # alice 是这个 project 的直接 owner
            s.add(UserProjectAccess(
                user_id=alice_id,
                project_id="test-proj",
                role="owner",
            ))
            await s.commit()

    asyncio.get_event_loop().run_until_complete(_seed_project())

    # 尝试删除 alice（她是 project 的 owner）→ 应该被拒绝
    r = client.delete(f"/admin/users/{alice_id}", headers=_auth(token))

    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    # 错误信息应该包含"工程"和"owner"相关字样
    assert "工程" in detail or "owner" in detail.lower()


# ─── 场景 11：删干净用户 → 204 ────────────────────────────────────────────

def test_delete_clean_user_ok(client_admin, session_maker):
    """删除一个没有 group/project ownership 的普通用户 → 204 No Content。"""
    client, token = client_admin

    # 先创建一个"干净"的用户（无任何 ownership）
    r_create = client.post(
        "/admin/users",
        json={
            "email": "temp@x.com",
            "username": "tempuser",
            "password": "12345678",
        },
        headers=_auth(token),
    )
    assert r_create.status_code == 201, r_create.text
    temp_user_id = r_create.json()["id"]

    # 删除这个干净用户
    r = client.delete(f"/admin/users/{temp_user_id}", headers=_auth(token))

    # 成功删除 → 204 No Content（无响应体）
    assert r.status_code == 204, r.text


def test_delete_nonexistent_user(client_admin):
    """删除不存在的用户 → 404 Not Found。"""
    client, token = client_admin

    r = client.delete("/admin/users/99999", headers=_auth(token))

    assert r.status_code == 404, r.text


def test_list_users_filter_by_admin_flag(client_admin):
    """GET /admin/users?is_admin=true 过滤出只有 admin 用户。"""
    client, token = client_admin

    r = client.get("/admin/users?is_admin=true", headers=_auth(token))

    assert r.status_code == 200, r.text
    data = r.json()
    # 所有返回用户的 is_admin 都应该是 True
    assert len(data) >= 1
    for u in data:
        assert u["is_admin"] is True


def test_update_nonexistent_user(client_admin):
    """PATCH 不存在的用户 → 404 Not Found。"""
    client, token = client_admin

    r = client.patch(
        "/admin/users/99999",
        json={"is_active": False},
        headers=_auth(token),
    )

    assert r.status_code == 404, r.text
