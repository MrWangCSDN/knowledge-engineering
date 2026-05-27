"""Project Members CRUD 路由测试（Task 9）。

测试场景清单：
1. owner 加 reporter → 201
2. maintainer 加成员 → 403
3. 加已存在成员 → 409
4. role 'super-admin' → 422
5. GET 返回 {direct: [...], inherited: [...]} 两组
6. GET inherited 自动包含 group 链上的成员
7. DELETE 删最后 direct owner，无 inherited owner → 422
8. DELETE 删非 owner → 204
9. PATCH 降级最后 owner 同样 422

被测接口：
  GET    /projects/{pid}/members           require_project_role("reporter")
  POST   /projects/{pid}/members           require_project_role("owner")
  PATCH  /projects/{pid}/members/{uid}     require_project_role("owner")
  DELETE /projects/{pid}/members/{uid}     require_project_role("owner")
"""

# ─── 标准库 / 第三方库导入 ─────────────────────────────────────────────────────

import asyncio          # 标准库：在同步测试中运行异步代码
import pytest           # pytest 测试框架：fixture、断言增强
import pytest_asyncio   # 扩展 pytest 支持 async fixture

from fastapi import FastAPI                             # FastAPI 应用类
from fastapi.testclient import TestClient               # 同步 HTTP 测试客户端
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,     # 异步 session 工厂
    create_async_engine,    # 创建异步数据库引擎
)
from sqlalchemy import select   # 构造 SELECT 查询语句

# ─── 项目内模块导入 ──────────────────────────────────────────────────────────────

from src.service import auth_security as sec                     # 密码 hash 工具
from src.service.auth_models import User                         # 用户 ORM 模型
from src.service.auth_router import router as auth_router        # 登录路由（/auth/login）
from src.service.db import Base, get_db                          # ORM Base + DB 依赖
from src.service.db_models_groups import Group, GroupMember      # Group / GroupMember ORM
from src.service.db_models_homepage import Project, UserProjectAccess  # Project / 成员 ORM
from src.service.project_member_router import router as project_member_router  # 被測 router
from src.service.deps_infra import require_infra_healthy  # Task 7：infra 健康检查 dep（测试里 no-op 覆盖）


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def session_maker(monkeypatch):
    """提供 in-memory SQLite 的异步 session 工厂，预建 3 个用户。

    用户：
      admin  (is_admin=True)   — Instance Admin
      alice  (is_admin=False)  — 普通用户，多数场景下会被加为 project owner
      bob    (is_admin=False)  — 普通用户，场景中作为被管理的 reporter/maintainer

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
    SM = async_sessionmaker(eng, expire_on_commit=False)

    # ─── seed 3 个用户 ──────────────────────────────────────────────────────
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

    yield SM  # yield 语法：把 SM 返回给调用方，测试结束后继续执行清理

    # ─── 测试结束清理 ──────────────────────────────────────────────────────
    await eng.dispose()


def _make_app(session_maker) -> FastAPI:
    """构造测试用 FastAPI app，注册 auth_router + project_member_router，并覆盖 get_db。

    Args:
        session_maker: async_sessionmaker，来自 session_maker fixture。

    Returns:
        FastAPI：装配好路由 + DI 覆盖的测试 app。
    """
    app = FastAPI()
    app.include_router(auth_router)            # 登录路由（/auth/login）
    app.include_router(project_member_router)  # 被测路由（/projects/{pid}/members/*）

    async def override_db():
        """覆盖 get_db：注入 in-memory SQLite session。

        async generator 函数：FastAPI 会把 yield 前后分别理解为"请求前"和"请求后"逻辑。
        """
        async with session_maker() as s:
            yield s
            await s.commit()

    # dependency_overrides：FastAPI 的 DI 覆盖机制
    app.dependency_overrides[get_db] = override_db
    # 测试里跳过 infra 健康检查（infra 不可用不是这批测试的验证目标）
    app.dependency_overrides[require_infra_healthy] = lambda: None
    return app


def _login(client: TestClient, username: str) -> str:
    """辅助函数：以指定用户名登录，返回 JWT access_token。

    Args:
        client: TestClient 实例。
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
    """辅助函数：构造 Authorization header dict。

    Args:
        token: JWT access_token。

    Returns:
        dict：{"Authorization": "Bearer <token>"}
    """
    return {"Authorization": f"Bearer {token}"}


# ─── client fixture ───────────────────────────────────────────────────────────

@pytest.fixture
def client(session_maker):
    """返回绑定了 in-memory SQLite 的 TestClient。

    Args:
        session_maker: in-memory SQLite session 工厂。

    Returns:
        TestClient：可直接发 HTTP 请求的测试客户端。
    """
    return TestClient(_make_app(session_maker))


@pytest.fixture
def client_admin(client):
    """已登录为 admin（Instance Admin）的 (client, token) 对。"""
    token = _login(client, "admin")
    return client, token


@pytest.fixture
def client_alice(client):
    """已登录为 alice（普通用户）的 (client, token) 对。"""
    token = _login(client, "alice")
    return client, token


@pytest.fixture
def client_bob(client):
    """已登录为 bob（普通用户）的 (client, token) 对。"""
    token = _login(client, "bob")
    return client, token


# ─── 辅助：查询用户 ID ─────────────────────────────────────────────────────────

def _get_user_id(session_maker, username: str) -> int:
    """同步辅助函数：查询指定用户名的 user_id（整数）。

    在同步 fixture 里用 asyncio.get_event_loop().run_until_complete(...)
    来运行异步数据库查询。

    Args:
        session_maker: 异步 session 工厂。
        username: 要查询的用户名。

    Returns:
        int：用户的整数 ID。
    """
    async def _query():
        async with session_maker() as s:
            user = await s.scalar(select(User).filter_by(username=username))
            return user.id

    return asyncio.get_event_loop().run_until_complete(_query())


# ─── 场景 fixture ──────────────────────────────────────────────────────────────

@pytest.fixture
def seed_project_alice_owner(client_admin, session_maker):
    """预建 'proj-alpha' project，alice 直接是 owner。

    场景用途：
      - 场景 1：alice（owner）加 bob → 201
      - 场景 3：加已存在成员 → 409
      - 场景 4：加无效 role → 422
      - 场景 7：删最后 direct owner → 422
      - 场景 8：owner 删 reporter → 204
      - 场景 9：PATCH 降级最后 owner → 422

    Returns:
        tuple：(client, admin_token, alice_token, bob_token, project_id)
    """
    client, admin_token = client_admin

    # ── 直接在 DB 里建 project + 加 alice 为 owner ──────────────────────────
    alice_id = _get_user_id(session_maker, "alice")
    admin_id = _get_user_id(session_maker, "admin")

    async def _seed():
        async with session_maker() as s:
            # 建 project（不通过 HTTP API，避免循环依赖）
            s.add(Project(
                id="proj-alpha",
                name="Alpha Project",
                language="java",
                status="ready",
                created_by="admin",
            ))
            # alice 直接成员，role = owner
            s.add(UserProjectAccess(
                user_id=alice_id,
                project_id="proj-alpha",
                role="owner",
            ))
            await s.commit()

    asyncio.get_event_loop().run_until_complete(_seed())

    alice_token = _login(client, "alice")
    bob_token = _login(client, "bob")
    return client, admin_token, alice_token, bob_token, "proj-alpha"


@pytest.fixture
def seed_project_alice_maintainer(client_admin, session_maker):
    """预建 'proj-beta' project，alice 是 maintainer。

    场景用途：
      - 场景 2：maintainer 加成员 → 403

    Returns:
        tuple：(client, alice_token, project_id)
    """
    client, admin_token = client_admin

    alice_id = _get_user_id(session_maker, "alice")

    async def _seed():
        async with session_maker() as s:
            s.add(Project(
                id="proj-beta",
                name="Beta Project",
                language="java",
                status="ready",
                created_by="admin",
            ))
            # alice 是 maintainer，不是 owner
            s.add(UserProjectAccess(
                user_id=alice_id,
                project_id="proj-beta",
                role="maintainer",
            ))
            await s.commit()

    asyncio.get_event_loop().run_until_complete(_seed())

    alice_token = _login(client, "alice")
    return client, alice_token, "proj-beta"


@pytest.fixture
def seed_project_with_group_members(client_admin, session_maker):
    """预建 'proj-gamma' + group 'team-a'，alice 是 project owner，bob 是 group member。

    继承关系：
      - group 'team-a' 有 bob（reporter）
      - project 'proj-gamma' 的 group_id = 'team-a'
      - 因此 bob 通过继承是 proj-gamma 的 reporter（inherited）

    场景用途：
      - 场景 5：GET 返回 {direct: [...], inherited: [...]}
      - 场景 6：GET inherited 自动包含 group 链上的成员

    Returns:
        tuple：(client, alice_token, bob_token, project_id)
    """
    client, admin_token = client_admin

    alice_id = _get_user_id(session_maker, "alice")
    bob_id = _get_user_id(session_maker, "bob")
    admin_id = _get_user_id(session_maker, "admin")

    async def _seed():
        async with session_maker() as s:
            # 建 group 'team-a'
            s.add(Group(
                id="team-a",
                name="Team A",
                parent_group_id=None,
                created_by_user_id=admin_id,
            ))
            # bob 是 group 成员（reporter）
            s.add(GroupMember(
                user_id=bob_id,
                group_id="team-a",
                role="reporter",
                added_by_user_id=admin_id,
            ))
            # 建 project 'proj-gamma'，归属 group 'team-a'
            s.add(Project(
                id="proj-gamma",
                name="Gamma Project",
                language="java",
                status="ready",
                group_id="team-a",   # 关联到 group，bob 通过继承可访问
                created_by="admin",
            ))
            # alice 是 project 直接成员（owner）
            s.add(UserProjectAccess(
                user_id=alice_id,
                project_id="proj-gamma",
                role="owner",
            ))
            await s.commit()

    asyncio.get_event_loop().run_until_complete(_seed())

    alice_token = _login(client, "alice")
    bob_token = _login(client, "bob")
    return client, alice_token, bob_token, "proj-gamma"


# ═══════════════════════════════════════════════════════════════════════════════
# 场景测试
# ═══════════════════════════════════════════════════════════════════════════════

# ─── 场景 1：owner 加 reporter → 201 ──────────────────────────────────────────

def test_owner_can_add_member(seed_project_alice_owner, session_maker):
    """POST /projects/{pid}/members：alice（owner）加 bob 为 reporter → 201。

    验证点：
      - 状态码 201
      - 响应体包含正确的 user_id 和 role
    """
    client, _, alice_token, _, pid = seed_project_alice_owner

    bob_id = _get_user_id(session_maker, "bob")
    r = client.post(
        f"/projects/{pid}/members",
        json={"user_id": bob_id, "role": "reporter"},
        headers=_auth(alice_token),
    )
    # 断言 201 Created
    assert r.status_code == 201, r.text

    body = r.json()
    # 响应体里要有 user_id 和 role 字段
    assert body["user_id"] == bob_id, f"user_id 不对: {body}"
    assert body["role"] == "reporter", f"role 不对: {body}"


# ─── 场景 2：maintainer 加成员 → 403 ─────────────────────────────────────────

def test_maintainer_cannot_add_member(seed_project_alice_maintainer, session_maker):
    """POST /projects/{pid}/members：alice（maintainer）加成员 → 403。

    加成员需要 owner 权限，maintainer 不够。
    """
    client, alice_token, pid = seed_project_alice_maintainer

    bob_id = _get_user_id(session_maker, "bob")
    r = client.post(
        f"/projects/{pid}/members",
        json={"user_id": bob_id, "role": "reporter"},
        headers=_auth(alice_token),
    )
    # maintainer 没有 owner 权限，应返回 403
    assert r.status_code == 403, r.text


# ─── 场景 3：加已存在成员 → 409 ──────────────────────────────────────────────

def test_add_duplicate_member_returns_409(seed_project_alice_owner, session_maker):
    """POST /projects/{pid}/members：加已存在的用户 → 409 Conflict。

    alice 已经是 'proj-alpha' 的 owner，再 POST 加 alice 应该返回 409。
    """
    client, _, alice_token, _, pid = seed_project_alice_owner

    alice_id = _get_user_id(session_maker, "alice")
    # alice 已是 owner，再加一次
    r = client.post(
        f"/projects/{pid}/members",
        json={"user_id": alice_id, "role": "reporter"},  # role 不同也应 409
        headers=_auth(alice_token),
    )
    # 已是成员，返回 409 Conflict
    assert r.status_code == 409, r.text


# ─── 场景 4：role 'super-admin' → 422 ────────────────────────────────────────

def test_add_member_with_invalid_role_returns_422(seed_project_alice_owner, session_maker):
    """POST /projects/{pid}/members：role 传 'super-admin'（非法值）→ 422。

    合法 role 只有 'reporter' / 'maintainer' / 'owner'，其他值应 422。
    """
    client, _, alice_token, _, pid = seed_project_alice_owner

    bob_id = _get_user_id(session_maker, "bob")
    r = client.post(
        f"/projects/{pid}/members",
        json={"user_id": bob_id, "role": "super-admin"},  # 非法 role
        headers=_auth(alice_token),
    )
    # 非法 role → 422 Unprocessable Entity（Pydantic 校验失败）
    assert r.status_code == 422, r.text


# ─── 场景 5：GET 返回 {direct: [...], inherited: [...]} 两组 ──────────────────

def test_get_members_returns_direct_and_inherited(seed_project_with_group_members):
    """GET /projects/{pid}/members：返回 {direct: [...], inherited: [...]} 结构。

    验证点：
      - 响应体是 dict，有 'direct' 和 'inherited' 两个 key
      - alice 在 direct 列表里（project 直接成员）
      - bob 在 inherited 列表里（通过 group 'team-a' 继承）
    """
    client, alice_token, bob_token, pid = seed_project_with_group_members

    r = client.get(
        f"/projects/{pid}/members",
        headers=_auth(alice_token),
    )
    assert r.status_code == 200, r.text

    body = r.json()
    # 响应体必须是 dict，含 direct 和 inherited 两个 key
    assert isinstance(body, dict), f"期望 dict，实际: {type(body)}"
    assert "direct" in body, f"缺少 'direct' key: {body}"
    assert "inherited" in body, f"缺少 'inherited' key: {body}"

    # direct 和 inherited 都是 list
    assert isinstance(body["direct"], list), f"direct 期望 list: {body}"
    assert isinstance(body["inherited"], list), f"inherited 期望 list: {body}"


# ─── 场景 6：GET inherited 自动包含 group 链上的成员 ──────────────────────────

def test_get_members_inherited_includes_group_member(
    seed_project_with_group_members, session_maker
):
    """GET /projects/{pid}/members：inherited 里包含 group 'team-a' 的成员 bob。

    验证点：
      - alice（project 直接 owner）在 direct 里
      - bob（group 'team-a' 的 reporter）在 inherited 里
    """
    client, alice_token, _, pid = seed_project_with_group_members

    alice_id = _get_user_id(session_maker, "alice")
    bob_id = _get_user_id(session_maker, "bob")

    r = client.get(
        f"/projects/{pid}/members",
        headers=_auth(alice_token),
    )
    assert r.status_code == 200, r.text

    body = r.json()
    direct_uids = [m["user_id"] for m in body["direct"]]
    inherited_uids = [m["user_id"] for m in body["inherited"]]

    # alice 在 direct 里
    assert alice_id in direct_uids, f"alice 不在 direct: {body}"
    # bob 在 inherited 里（通过 group 'team-a' 继承）
    assert bob_id in inherited_uids, f"bob 不在 inherited: {body}"


# ─── 场景 7：DELETE 删最后 direct owner，无 inherited owner → 422 ─────────────

def test_delete_last_direct_owner_returns_422(seed_project_alice_owner, session_maker):
    """DELETE /projects/{pid}/members/{uid}：删最后一个 direct owner → 422。

    场景：
      - proj-alpha 只有 alice 一个 direct owner
      - alice 试图删自己（唯一 owner）→ 422

    注意：这里 admin（is_admin=True）是 Instance Admin，不算"直接成员"，
    所以 alice 确实是唯一 direct owner。
    """
    client, _, alice_token, _, pid = seed_project_alice_owner

    alice_id = _get_user_id(session_maker, "alice")

    # alice 试图删自己（唯一 direct owner）
    r = client.delete(
        f"/projects/{pid}/members/{alice_id}",
        headers=_auth(alice_token),
    )
    # 唯一 direct owner 不可删 → 422
    assert r.status_code == 422, r.text
    # 错误信息里要包含"owner"关键词
    assert "owner" in r.json()["detail"].lower(), r.json()


# ─── 场景 8：DELETE 删非 owner → 204 ─────────────────────────────────────────

def test_owner_can_delete_reporter(seed_project_alice_owner, session_maker):
    """DELETE /projects/{pid}/members/{uid}：owner 删 reporter → 204。

    步骤：
      1. alice（owner）先加 bob 为 reporter
      2. alice 再删 bob
      3. 验证响应 204
    """
    client, _, alice_token, _, pid = seed_project_alice_owner

    bob_id = _get_user_id(session_maker, "bob")

    # 先加 bob 为 reporter
    r_add = client.post(
        f"/projects/{pid}/members",
        json={"user_id": bob_id, "role": "reporter"},
        headers=_auth(alice_token),
    )
    assert r_add.status_code == 201, f"加成员失败: {r_add.text}"

    # alice 删 bob
    r = client.delete(
        f"/projects/{pid}/members/{bob_id}",
        headers=_auth(alice_token),
    )
    # 成功删除 → 204 No Content（无响应体）
    assert r.status_code == 204, r.text


# ─── 场景 9：PATCH 降级最后 owner 同样 422 ────────────────────────────────────

def test_patch_downgrade_last_owner_returns_422(seed_project_alice_owner, session_maker):
    """PATCH /projects/{pid}/members/{uid}：降级最后 direct owner → 422。

    场景：
      - proj-alpha 只有 alice 一个 direct owner
      - alice 试图把自己 PATCH 为 maintainer（降级）→ 422
    """
    client, _, alice_token, _, pid = seed_project_alice_owner

    alice_id = _get_user_id(session_maker, "alice")

    # alice 试图把自己从 owner 降为 maintainer
    r = client.patch(
        f"/projects/{pid}/members/{alice_id}",
        json={"role": "maintainer"},
        headers=_auth(alice_token),
    )
    # 最后一个 direct owner 不可降级 → 422
    assert r.status_code == 422, r.text
    assert "owner" in r.json()["detail"].lower(), r.json()


# ─── 额外场景：PATCH 改成员 role → 200 ───────────────────────────────────────

def test_owner_can_patch_member_role(seed_project_alice_owner, session_maker):
    """PATCH /projects/{pid}/members/{uid}：owner 改 bob 的 role → 200。

    步骤：
      1. alice 加 bob 为 reporter
      2. alice PATCH bob 为 maintainer
      3. 验证响应 200 + role = maintainer
    """
    client, _, alice_token, _, pid = seed_project_alice_owner

    bob_id = _get_user_id(session_maker, "bob")

    # 先加 bob 为 reporter
    r_add = client.post(
        f"/projects/{pid}/members",
        json={"user_id": bob_id, "role": "reporter"},
        headers=_auth(alice_token),
    )
    assert r_add.status_code == 201, f"加成员失败: {r_add.text}"

    # 再把 bob 改为 maintainer
    r = client.patch(
        f"/projects/{pid}/members/{bob_id}",
        json={"role": "maintainer"},
        headers=_auth(alice_token),
    )
    assert r.status_code == 200, r.text

    # 验证响应体里 role 确实改为 maintainer
    body = r.json()
    assert body["role"] == "maintainer", f"role 改错了: {body}"
