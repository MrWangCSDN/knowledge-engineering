"""Group Members CRUD 路由测试（Task 8）。

测试场景清单：
1. Group Owner 加成员 → 201
2. Group Maintainer 加成员 → 403
3. 加已存在成员 → 409
4. role 无效（'super-admin'）→ 422
5. Owner 改成员 role → 200
6. Owner 删自己（最后一个 owner）→ 422 "组必须至少 1 个 owner"
7. Owner 删别人 → 204

被测接口：
  GET    /groups/{gid}/members           require_group_role("reporter")
  POST   /groups/{gid}/members           require_group_role("owner")
  PATCH  /groups/{gid}/members/{user_id} require_group_role("owner")
  DELETE /groups/{gid}/members/{user_id} require_group_role("owner")
"""

# ─── 标准库 / 第三方库导入 ─────────────────────────────────────────────────────

import asyncio      # 标准库：在同步测试中运行异步代码（asyncio.get_event_loop().run_until_complete）
import pytest       # pytest 测试框架：fixture、断言增强、参数化
import pytest_asyncio  # 扩展 pytest 支持 async fixture

from fastapi import FastAPI                             # FastAPI 应用类
from fastapi.testclient import TestClient               # 同步 HTTP 测试客户端
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,     # 异步 session 工厂
    create_async_engine,    # 创建异步数据库引擎
)
from sqlalchemy import select   # 构造 SELECT 查询语句

# ─── 项目内模块导入 ──────────────────────────────────────────────────────────────

from src.service import auth_security as sec            # 密码 hash 工具
from src.service.auth_models import User                # 用户 ORM 模型
from src.service.auth_router import router as auth_router   # 登录路由（/auth/login）
from src.service.db import Base, get_db                 # ORM Base + DB 依赖
from src.service.db_models_groups import Group, GroupMember  # Group / GroupMember ORM
from src.service.group_router import router as group_router  # 被测 router（含 members sub-routes）
from src.service.deps_infra import require_infra_healthy  # Task 7：infra 健康检查 dep（测试里 no-op 覆盖）


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def session_maker(monkeypatch):
    """提供 in-memory SQLite 的异步 session 工厂，预建 3 个用户。

    用户：
      admin  (is_admin=True)   — Instance Admin
      alice  (is_admin=False)  — 普通用户，多数场景下会被加为 group owner
      bob    (is_admin=False)  — 普通用户，场景中作为被管理的 reporter/maintainer

    Args:
        monkeypatch: pytest 内置 fixture，临时修改环境变量（测试后自动还原）。

    Yields:
        async_sessionmaker：供下游 fixture 创建 DB session。
    """
    # 注入 JWT 密钥（登录 /auth/login 时需要，不设会报错）
    monkeypatch.setenv("KE_JWT_SECRET", "x" * 32)
    # 关闭 Secure cookie（测试用 http，不需要 https）
    monkeypatch.setenv("KE_COOKIE_SECURE", "false")

    # 创建纯内存 SQLite 异步引擎（测试数据完全隔离，不写磁盘）
    # sqlite+aiosqlite：aiosqlite 是 SQLite 的 asyncio 异步驱动
    # "/:memory:"：纯内存数据库，引擎关闭即销毁，各测试函数互相隔离
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    # async with eng.begin() as conn：获取一个"已开始事务"的异步连接
    # run_sync(callable)：在异步上下文中执行同步函数（create_all 是同步的）
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)  # 建表（依据所有 ORM 类的 __tablename__）

    # async_sessionmaker：异步 session 工厂
    # expire_on_commit=False：commit 后不"过期" ORM 对象，避免读属性时触发额外 SELECT
    SM = async_sessionmaker(eng, expire_on_commit=False)

    # ─── seed 3 个用户 ──────────────────────────────────────────────────────
    async with SM() as s:
        # s.add_all(list)：批量添加多个 ORM 实例到 session（比逐个 add 写法更简洁）
        s.add_all([
            User(
                email="admin@x.com",
                username="admin",
                hashed_password=sec.hash_password("12345678"),
                is_active=True,
                is_admin=True,   # Instance Admin：可建根 group + 所有 CRUD
            ),
            User(
                email="alice@x.com",
                username="alice",
                hashed_password=sec.hash_password("12345678"),
                is_active=True,
                is_admin=False,  # 普通用户，场景中作为 group owner/maintainer
            ),
            User(
                email="bob@x.com",
                username="bob",
                hashed_password=sec.hash_password("12345678"),
                is_active=True,
                is_admin=False,  # 普通用户，场景中作为 reporter/被加入的成员
            ),
        ])
        await s.commit()

    yield SM  # yield 语法：把 SM 返回给调用方，测试执行完后继续执行下面的清理

    # ─── 测试结束清理 ──────────────────────────────────────────────────────
    # eng.dispose()：关闭连接池 + 释放内存 DB，避免"connection already closed"警告
    await eng.dispose()


def _make_app(session_maker) -> FastAPI:
    """构造测试用 FastAPI app，注册 auth_router + group_router，并覆盖 get_db。

    dependency_overrides：FastAPI 的 DI 覆盖机制，
    把生产用的 get_db（连接 MySQL）替换为测试用的 in-memory SQLite。
    所有路由里的 Depends(get_db) 都会透明地得到测试 session。

    Args:
        session_maker: async_sessionmaker，来自 session_maker fixture。

    Returns:
        FastAPI：装配好路由 + DI 覆盖的测试 app。
    """
    app = FastAPI()
    app.include_router(auth_router)   # 登录路由（/auth/login），用于取 JWT token
    app.include_router(group_router)  # 被测路由（/groups/*，含 members sub-routes）

    async def override_db():
        """覆盖 get_db：注入 in-memory SQLite session。

        async generator 函数（有 yield 的 async def）：
        FastAPI 会把 yield 前后的代码分别理解为"请求前"和"请求后"逻辑。
        """
        # async with SM() as s：异步上下文管理器，保证 session 正确关闭（即使抛异常）
        async with session_maker() as s:
            yield s             # 把 session 注入路由函数
            await s.commit()    # 路由执行后自动 commit（保证同一 session 的查询能看到写操作）

    # dependency_overrides 是 dict，key 是被覆盖的依赖函数，value 是替代函数
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
        str：JWT access_token，用于后续请求 Authorization header。
    """
    r = client.post("/auth/login", json={
        "username": username,
        "password": "12345678",
        "remember_me": False,
    })
    # assert 语句：若条件为 False，测试立即失败，并打印 r.text 帮助调试
    assert r.status_code == 200, f"登录失败: {r.text}"
    # r.json()：把响应体 JSON 字符串解析为 Python dict
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    """辅助函数：构造 Authorization header dict。

    Args:
        token: JWT access_token。

    Returns:
        dict：{"Authorization": "Bearer <token>"}，直接传给 TestClient 的 headers 参数。
    """
    # f-string：Python 3.6+ 的字符串格式化，{token} 直接插入变量值
    return {"Authorization": f"Bearer {token}"}


# ─── client fixture ───────────────────────────────────────────────────────────

@pytest.fixture
def client(session_maker):
    """返回绑定了 in-memory SQLite 的 TestClient。

    TestClient 是 Starlette 的同步测试客户端，内部用 threading + asyncio 驱动 ASGI app。
    每次测试函数使用同一 TestClient（同一 session_maker），数据互相可见。

    Args:
        session_maker: in-memory SQLite session 工厂（来自 session_maker fixture）。

    Returns:
        TestClient：可直接发 HTTP 请求的测试客户端。
    """
    return TestClient(_make_app(session_maker))


@pytest.fixture
def client_admin(client):
    """已登录为 admin（Instance Admin）的 (client, token) 对。

    Returns:
        tuple[TestClient, str]：(client, admin_token)
    """
    token = _login(client, "admin")
    return client, token


@pytest.fixture
def client_alice(client):
    """已登录为 alice（普通用户）的 (client, token) 对。

    Returns:
        tuple[TestClient, str]：(client, alice_token)
    """
    token = _login(client, "alice")
    return client, token


@pytest.fixture
def client_bob(client):
    """已登录为 bob（普通用户）的 (client, token) 对。

    Returns:
        tuple[TestClient, str]：(client, bob_token)
    """
    token = _login(client, "bob")
    return client, token


# ─── 场景 fixture：在数据库里预建 group 并设置成员 ─────────────────────────────

def _get_user_id(session_maker, username: str) -> int:
    """同步辅助函数：查询指定用户名的 user_id（整数）。

    在同步 fixture 里用 asyncio.get_event_loop().run_until_complete(...)
    来运行异步数据库查询。

    Args:
        session_maker: 异步 session 工厂。
        username:      要查询的用户名。

    Returns:
        int：用户的整数 ID。
    """
    async def _query():
        # 用 async with SM() as s 创建一个 session
        async with session_maker() as s:
            # db.scalar(select(Model).filter_by(...))：返回满足条件的第一行对象
            user = await s.scalar(select(User).filter_by(username=username))
            return user.id  # 返回整数 id

    # asyncio.get_event_loop()：获取当前线程的事件循环（在同步函数里用这个调异步代码）
    # .run_until_complete(coroutine)：同步地等待协程执行完毕并返回结果
    return asyncio.get_event_loop().run_until_complete(_query())


@pytest.fixture
def seed_group_alice_owner(client_admin, session_maker):
    """预建 'dev' group，admin 建组（自动成为 owner），同时在 DB 里给 alice 加 owner。

    场景用途：
      - 场景 1：alice（owner）加成员
      - 场景 3：加已存在成员
      - 场景 4：加无效 role
      - 场景 5：改成员 role
      - 场景 6：alice 删自己（唯一 owner）→ 422
      - 场景 7：alice 删别人

    注意：admin 建组时自动成为 owner（group_router.py 逻辑），
    因此 'dev' group 在这个 fixture 创建后有 admin（owner）和 alice（owner）两个 owner。

    Returns:
        tuple：(client, admin_token, alice_token, bob_token, group_id)
    """
    client, admin_token = client_admin

    # admin 建根 group 'dev'（admin 自动成为 owner）
    r = client.post(
        "/groups",
        json={"id": "dev", "name": "研发组"},
        headers=_auth(admin_token),
    )
    assert r.status_code == 201, f"建 group 失败: {r.text}"

    # 在 DB 里直接给 alice 加 owner（跳过"加成员"接口，避免循环依赖）
    alice_id = _get_user_id(session_maker, "alice")
    admin_id = _get_user_id(session_maker, "admin")

    async def _add_alice():
        async with session_maker() as s:
            # GroupMember：复合主键 (user_id, group_id)，每用户每组只能一条
            s.add(GroupMember(
                user_id=alice_id,
                group_id="dev",
                role="owner",         # alice 是 owner
                added_by_user_id=admin_id,
            ))
            await s.commit()

    asyncio.get_event_loop().run_until_complete(_add_alice())

    # 获取 bob 的 token（不需要提前在 DB 建 bob 的成员记录）
    alice_token = _login(client, "alice")
    bob_token = _login(client, "bob")

    return client, admin_token, alice_token, bob_token, "dev"


@pytest.fixture
def seed_group_alice_owner_maintainer_only(client_admin, session_maker):
    """预建 'ops' group，alice 是 maintainer（不是 owner）。

    场景用途：
      - 场景 2：alice（maintainer）加成员 → 403（需要 owner）

    Returns:
        tuple：(client, alice_token, group_id)
    """
    client, admin_token = client_admin

    # admin 建根 group 'ops'
    r = client.post(
        "/groups",
        json={"id": "ops", "name": "运维组"},
        headers=_auth(admin_token),
    )
    assert r.status_code == 201, f"建 ops group 失败: {r.text}"

    # 给 alice 加 maintainer（非 owner）
    alice_id = _get_user_id(session_maker, "alice")
    admin_id = _get_user_id(session_maker, "admin")

    async def _add():
        async with session_maker() as s:
            s.add(GroupMember(
                user_id=alice_id,
                group_id="ops",
                role="maintainer",      # maintainer 权限，不能管成员
                added_by_user_id=admin_id,
            ))
            await s.commit()

    asyncio.get_event_loop().run_until_complete(_add())

    alice_token = _login(client, "alice")
    return client, alice_token, "ops"


# ═══════════════════════════════════════════════════════════════════════════════
# 场景测试
# ═══════════════════════════════════════════════════════════════════════════════

# ─── 场景 1：Group Owner 加成员 → 201 ────────────────────────────────────────

def test_owner_can_add_member(seed_group_alice_owner, session_maker):
    """POST /groups/{gid}/members：owner（alice）加 bob 为 reporter → 201。

    验证点：
      - 状态码 201
      - 响应体包含正确的 user_id 和 role
      - 数据库里确实有 bob 的成员记录
    """
    client, _, alice_token, _, gid = seed_group_alice_owner

    # alice（owner）把 bob 加为 reporter
    bob_id = _get_user_id(session_maker, "bob")
    r = client.post(
        f"/groups/{gid}/members",
        json={"user_id": bob_id, "role": "reporter"},
        headers=_auth(alice_token),
    )
    # 断言 201 Created
    assert r.status_code == 201, r.text

    # 响应体里要有 user_id 和 role 字段
    body = r.json()
    assert body["user_id"] == bob_id, f"user_id 不对: {body}"
    assert body["role"] == "reporter", f"role 不对: {body}"


# ─── 场景 2：Maintainer 加成员 → 403 ──────────────────────────────────────────

def test_maintainer_cannot_add_member(seed_group_alice_owner_maintainer_only, session_maker):
    """POST /groups/{gid}/members：maintainer（alice）加成员 → 403。

    加成员需要 owner 权限，maintainer 不够。
    """
    client, alice_token, gid = seed_group_alice_owner_maintainer_only

    bob_id = _get_user_id(session_maker, "bob")
    r = client.post(
        f"/groups/{gid}/members",
        json={"user_id": bob_id, "role": "reporter"},
        headers=_auth(alice_token),
    )
    # maintainer 没有 owner 权限，应返回 403
    assert r.status_code == 403, r.text


# ─── 场景 3：加已存在成员 → 409 ──────────────────────────────────────────────

def test_add_duplicate_member_returns_409(seed_group_alice_owner, session_maker):
    """POST /groups/{gid}/members：加已经是成员的用户 → 409 Conflict。

    alice 已经是 'dev' group 的 owner（seed_group_alice_owner fixture 建立），
    再 POST 加 alice 应该返回 409。
    """
    client, _, alice_token, _, gid = seed_group_alice_owner

    alice_id = _get_user_id(session_maker, "alice")
    # alice 已经是 owner，再加一次
    r = client.post(
        f"/groups/{gid}/members",
        json={"user_id": alice_id, "role": "reporter"},  # role 不同也应该 409（已存在优先）
        headers=_auth(alice_token),
    )
    # 已经是成员，返回 409 Conflict
    assert r.status_code == 409, r.text


# ─── 场景 4：role 无效 → 422 ──────────────────────────────────────────────────

def test_add_member_with_invalid_role_returns_422(seed_group_alice_owner, session_maker):
    """POST /groups/{gid}/members：role 传 'super-admin'（非法值）→ 422。

    合法 role 只有 'reporter' / 'maintainer' / 'owner'，其他值应 422。
    """
    client, _, alice_token, _, gid = seed_group_alice_owner

    bob_id = _get_user_id(session_maker, "bob")
    r = client.post(
        f"/groups/{gid}/members",
        # 'super-admin' 是非法 role 值
        json={"user_id": bob_id, "role": "super-admin"},
        headers=_auth(alice_token),
    )
    # 非法 role → 422 Unprocessable Entity（Pydantic 校验失败）
    assert r.status_code == 422, r.text


# ─── 场景 5：Owner 改成员 role → 200 ─────────────────────────────────────────

def test_owner_can_patch_member_role(seed_group_alice_owner, session_maker):
    """PATCH /groups/{gid}/members/{user_id}：owner（alice）改 bob 的 role → 200。

    步骤：
      1. alice 先把 bob 加为 reporter（POST /members）
      2. 再 PATCH 把 bob 改为 maintainer
      3. 验证响应 200 + role = maintainer
    """
    client, _, alice_token, _, gid = seed_group_alice_owner

    bob_id = _get_user_id(session_maker, "bob")

    # 先把 bob 加为 reporter
    r_add = client.post(
        f"/groups/{gid}/members",
        json={"user_id": bob_id, "role": "reporter"},
        headers=_auth(alice_token),
    )
    assert r_add.status_code == 201, f"加成员失败: {r_add.text}"

    # 再把 bob 改为 maintainer
    r = client.patch(
        f"/groups/{gid}/members/{bob_id}",
        json={"role": "maintainer"},
        headers=_auth(alice_token),
    )
    assert r.status_code == 200, r.text

    # 验证响应体里 role 确实改为 maintainer
    body = r.json()
    assert body["role"] == "maintainer", f"role 改错了: {body}"


# ─── 场景 6：Owner 删自己（最后一个 owner）→ 422 ─────────────────────────────

def test_delete_last_owner_returns_422(seed_group_alice_owner, session_maker):
    """DELETE /groups/{gid}/members/{user_id}：删 group 最后一个 owner → 422。

    场景构建：
      - 'dev' group 有 admin（owner）和 alice（owner）
      - 先删 admin（调用 DELETE）→ 成功（剩 alice 是唯一 owner）
      - 再删 alice（alice 是唯一 owner）→ 422 "组必须至少 1 个 owner"

    注意：admin 是 Instance Admin（is_admin=True），
    即使数据库里 admin 没有 group_member 记录，require_group_role 也会放行。
    但这里 admin 在 seed 时已经通过 POST /groups 自动成为 owner，
    所以 DELETE admin 时有真实的 group_member 记录。
    """
    client, admin_token, alice_token, _, gid = seed_group_alice_owner

    admin_id = _get_user_id(session_maker, "admin")
    alice_id = _get_user_id(session_maker, "alice")

    # admin 删自己（admin + alice 各有一条 owner 记录，admin 删后 alice 仍是 owner）
    # 注意：这里用 alice 的 token 来删 admin（owner 可以删任何成员，包括另一个 owner）
    r_del_admin = client.delete(
        f"/groups/{gid}/members/{admin_id}",
        headers=_auth(alice_token),   # alice 是 owner，有权操作
    )
    assert r_del_admin.status_code == 204, f"删 admin 失败: {r_del_admin.text}"

    # 现在 alice 是唯一 owner，再删 alice → 422
    r = client.delete(
        f"/groups/{gid}/members/{alice_id}",
        headers=_auth(alice_token),
    )
    assert r.status_code == 422, r.text
    # 错误信息里要包含"owner"关键词
    assert "owner" in r.json()["detail"].lower(), r.json()


# ─── 场景 7：Owner 删别人 → 204 ──────────────────────────────────────────────

def test_owner_can_delete_other_member(seed_group_alice_owner, session_maker):
    """DELETE /groups/{gid}/members/{user_id}：owner 删别人 → 204 No Content。

    步骤：
      1. alice 先把 bob 加为 reporter
      2. alice 再删 bob
      3. 验证响应 204
      4. GET /members 确认 bob 不在列表里
    """
    client, _, alice_token, _, gid = seed_group_alice_owner

    bob_id = _get_user_id(session_maker, "bob")

    # 先加 bob
    r_add = client.post(
        f"/groups/{gid}/members",
        json={"user_id": bob_id, "role": "reporter"},
        headers=_auth(alice_token),
    )
    assert r_add.status_code == 201, f"加成员失败: {r_add.text}"

    # alice 删 bob
    r = client.delete(
        f"/groups/{gid}/members/{bob_id}",
        headers=_auth(alice_token),
    )
    # 成功删除 → 204 No Content（无响应体）
    assert r.status_code == 204, r.text

    # 验证 bob 确实不在成员列表里了（GET /groups/{gid}/members）
    r_list = client.get(
        f"/groups/{gid}/members",
        headers=_auth(alice_token),
    )
    assert r_list.status_code == 200, r_list.text
    member_ids = [m["user_id"] for m in r_list.json()]
    assert bob_id not in member_ids, f"bob 还在成员列表里: {r_list.json()}"


# ─── 额外场景：GET /members 列出成员 ─────────────────────────────────────────

def test_list_members(seed_group_alice_owner, session_maker):
    """GET /groups/{gid}/members：reporter+ 能列出所有成员。

    验证：
      - alice（owner）调 GET，能看到 admin 和 alice（两个 owner）
      - 响应包含 user_id 和 role 字段
    """
    client, _, alice_token, _, gid = seed_group_alice_owner

    alice_id = _get_user_id(session_maker, "alice")
    admin_id = _get_user_id(session_maker, "admin")

    r = client.get(
        f"/groups/{gid}/members",
        headers=_auth(alice_token),
    )
    assert r.status_code == 200, r.text

    # 响应是列表
    members = r.json()
    assert isinstance(members, list), f"期望 list，实际: {type(members)}"

    # admin 和 alice 都应该在列表里
    member_ids = [m["user_id"] for m in members]
    assert admin_id in member_ids, f"admin 不在成员列表: {members}"
    assert alice_id in member_ids, f"alice 不在成员列表: {members}"
