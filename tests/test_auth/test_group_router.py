"""Groups CRUD 路由测试。

测试场景清单：
1. POST /groups: Instance Admin 能建根 group → 201
2. POST /groups: 非 admin 想建根 group → 403
3. POST /groups: 在 parent_group_id 下 Group Owner 能建子组 → 201
4. POST /groups: 嵌套深度 4 → 422 "嵌套深度超过 3 层"
5. POST /groups: parent_group_id 不存在 → 404
6. GET /groups: 返回用户可见的 group 树
7. PATCH /groups/{gid}: maintainer 能改 name
8. PATCH /groups/{gid}: reporter 不能改 → 403
9. DELETE /groups/{gid}: 有子组 → 403 "先删子组"
10. DELETE /groups/{gid}: 有工程 → 403 "先迁移工程"
11. DELETE /groups/{gid}: 干净时 owner 能删 → 204
"""

# ─── 标准库 / 第三方库导入 ─────────────────────────────────────────────────────

import os           # 标准库：读取 / 设置环境变量
import pytest       # pytest 测试框架：提供 fixture、断言增强、参数化
import pytest_asyncio  # 扩展 pytest 支持 async fixture

from fastapi import FastAPI                             # FastAPI 应用类
from fastapi.testclient import TestClient               # 同步 HTTP 测试客户端
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,     # 异步 session 工厂
    create_async_engine,    # 创建异步数据库引擎
)

# ─── 项目内模块导入 ──────────────────────────────────────────────────────────────

from src.service import auth_security as sec            # 密码 hash 工具
from src.service.auth_models import User                # 用户 ORM 模型
from src.service.auth_router import router as auth_router  # 登录路由
from src.service.db import Base, get_db                 # ORM Base + DB 依赖
from src.service.db_models_groups import Group, GroupMember  # Group / GroupMember ORM
from src.service.db_models_homepage import Project      # Project ORM（用于 delete-with-projects 场景）
from src.service.group_router import router as group_router  # 被测 router（Task 7 实现）
from src.service.deps_infra import require_infra_healthy  # Task 7：infra 健康检查 dep（测试里 no-op 覆盖）


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def session_maker(monkeypatch):
    """提供 in-memory SQLite 的异步 session 工厂，预建 3 个用户。

    用户列表：
      admin  (is_admin=True)   — Instance Admin
      alice  (is_admin=False)  — 普通用户，稍后会被某些 fixture 加为 group owner
      bob    (is_admin=False)  — 普通用户（reporter 角色）

    monkeypatch：pytest 内置 fixture，用于临时修改环境变量，测试结束后自动还原。

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
    # sqlite+aiosqlite：aiosqlite 是 SQLite 的异步驱动
    # ":memory:"：纯内存数据库，进程退出 / 引擎关闭即销毁
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    # run_sync：在异步上下文里执行同步函数（create_all 是同步的）
    # Base.metadata.create_all：根据所有继承自 Base 的 ORM 类，创建对应的数据库表
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # async_sessionmaker：异步 session 工厂
    # expire_on_commit=False：commit 后不过期 ORM 对象，避免访问属性时触发额外 SELECT
    SM = async_sessionmaker(eng, expire_on_commit=False)

    # ─── seed 3 个用户 ──────────────────────────────────────────────────────
    async with SM() as s:
        s.add_all([
            User(
                email="admin@x.com",
                username="admin",
                hashed_password=sec.hash_password("12345678"),
                is_active=True,
                is_admin=True,   # Instance Admin：可建根 group、看所有内容
            ),
            User(
                email="alice@x.com",
                username="alice",
                hashed_password=sec.hash_password("12345678"),
                is_active=True,
                is_admin=False,  # 普通用户，后续 fixture 会赋予 group owner
            ),
            User(
                email="bob@x.com",
                username="bob",
                hashed_password=sec.hash_password("12345678"),
                is_active=True,
                is_admin=False,  # 普通用户，后续 fixture 会赋予 reporter 角色
            ),
        ])
        await s.commit()

    yield SM  # yield 把 SM 注入给依赖此 fixture 的 fixture

    # ─── 清理 ─────────────────────────────────────────────────────────────
    # 关闭引擎（释放连接池 + 内存 DB），避免"connection already closed"警告
    await eng.dispose()


def _make_app(session_maker) -> FastAPI:
    """构造测试用 FastAPI app，注册 auth_router + group_router，并覆盖 get_db。

    dependency_overrides：FastAPI 机制，把 get_db 替换为 in-memory SQLite，
    所有路由函数里的 Depends(get_db) 都会透明地得到测试 session。

    Args:
        session_maker: async_sessionmaker，来自 session_maker fixture。

    Returns:
        FastAPI：装配好路由和依赖覆盖的测试 app。
    """
    app = FastAPI()
    app.include_router(auth_router)   # 登录路由（/auth/login），用于取 JWT token
    app.include_router(group_router)  # 被测路由（/groups/*）

    async def override_db():
        """覆盖 get_db：注入 in-memory SQLite session。"""
        # async with SM() as s：异步上下文管理器，保证 session 正确关闭
        async with session_maker() as s:
            yield s             # 把 session 注入路由函数
            await s.commit()    # 路由执行后自动 commit（保证测试内数据可查到）

    # 把 get_db 替换为 override_db
    app.dependency_overrides[get_db] = override_db
    # 测试里跳过 infra 健康检查（infra 不可用不是这批测试的验证目标）
    app.dependency_overrides[require_infra_healthy] = lambda: None
    return app


def _login(client: TestClient, username: str) -> str:
    """辅助函数：以指定用户登录，返回 JWT access_token。

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
    assert r.status_code == 200, f"登录失败: {r.text}"
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    """辅助函数：构造 Authorization header dict。

    Args:
        token: JWT access_token。

    Returns:
        dict：{"Authorization": "Bearer <token>"}，直接传给 TestClient.headers。
    """
    return {"Authorization": f"Bearer {token}"}


# ─── client fixture ───────────────────────────────────────────────────────────

@pytest.fixture
def client(session_maker):
    """返回绑定了 in-memory SQLite 的 TestClient。

    TestClient 是 Starlette 同步测试客户端，内部用 threading + asyncio 执行 ASGI。
    每次测试函数使用同一 TestClient（同一 session_maker），数据互相可见。
    如需数据隔离，每个测试用不同 session_maker（参数化 fixture）。

    Args:
        session_maker: in-memory SQLite session 工厂。

    Returns:
        TestClient：可直接发 HTTP 请求。
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


# ─── 场景 fixture：在数据库里预建 group 数据 ──────────────────────────────────

@pytest.fixture
def seed_root_group_alice_owner(client_admin, client_alice, session_maker):
    """预建 'retail-bank' 根 group，并让 alice 成为 owner。

    步骤：
      1. admin 用 POST /groups 建根 group（admin 有权限）→ alice 不在成员列表
      2. 直接在 DB 里为 alice 插一条 owner 成员记录（跳过 admin 自己的 owner 记录）

    说明：
      admin 创建 group 时会自动成为 owner（group_router.py 逻辑），
      但 alice 需要额外在 DB 里加成员记录，这里直接操作 DB，
      避免依赖"加成员"接口（Task 8 实现）。

    Args:
        client_admin: (client, admin_token) tuple
        client_alice: (client, alice_token) tuple，仅用于取 alice 的 user_id
        session_maker: in-memory SQLite session 工厂，用于直接写 DB

    Returns:
        str：group id（'retail-bank'）
    """
    client, admin_token = client_admin
    _, alice_token = client_alice

    # admin 建根 group
    r = client.post(
        "/groups",
        json={"id": "retail-bank", "name": "零售银行"},
        headers=_auth(admin_token),
    )
    assert r.status_code == 201, f"admin 建根 group 失败: {r.text}"

    # 直接在 DB 里把 alice 加为 owner（alice 的 user_id 是 2，因为 seed 顺序：admin=1, alice=2, bob=3）
    import asyncio

    async def _add_alice_owner():
        # 查 alice 的 user_id
        from sqlalchemy import select
        from src.service.auth_models import User as UserModel
        async with session_maker() as s:
            alice = await s.scalar(select(UserModel).filter_by(username="alice"))
            s.add(GroupMember(
                user_id=alice.id,
                group_id="retail-bank",
                role="owner",
                added_by_user_id=alice.id,
            ))
            await s.commit()

    asyncio.get_event_loop().run_until_complete(_add_alice_owner())
    return "retail-bank"


@pytest.fixture
def seed_2_level_groups(client_admin):
    """预建 2 层 group：root → root/child。

    这个 fixture 用于"有子组时不能删父组"的场景测试。

    Args:
        client_admin: (client, admin_token) tuple

    Returns:
        tuple[str, str]：(root_id, child_id)
    """
    client, token = client_admin

    # 建根 group
    r = client.post(
        "/groups",
        json={"id": "root", "name": "根组"},
        headers=_auth(token),
    )
    assert r.status_code == 201, f"建根 group 失败: {r.text}"

    # 建子 group（parent_group_id = "root"）
    r = client.post(
        "/groups",
        json={"id": "root/child", "name": "子组", "parent_group_id": "root"},
        headers=_auth(token),
    )
    assert r.status_code == 201, f"建子 group 失败: {r.text}"

    return "root", "root/child"


@pytest.fixture
def seed_3_level_groups(client_admin):
    """预建 3 层 group：root → root/lv1 → root/lv1/lv2。

    这个 fixture 用于"嵌套深度超过 3 层"场景：
    已有 3 层（root 是第 1 层，lv2 是第 3 层），
    再在 lv2 下建子组 = 第 4 层，应被拒绝。

    Args:
        client_admin: (client, admin_token) tuple

    Returns:
        tuple[str, str, str]：(root_id, lv1_id, lv2_id)
    """
    client, token = client_admin

    # 第 1 层：根 group
    r = client.post(
        "/groups",
        json={"id": "root", "name": "根"},
        headers=_auth(token),
    )
    assert r.status_code == 201, f"建 root 失败: {r.text}"

    # 第 2 层：lv1
    r = client.post(
        "/groups",
        json={"id": "root/lv1", "name": "一级", "parent_group_id": "root"},
        headers=_auth(token),
    )
    assert r.status_code == 201, f"建 lv1 失败: {r.text}"

    # 第 3 层：lv2
    r = client.post(
        "/groups",
        json={"id": "root/lv1/lv2", "name": "二级", "parent_group_id": "root/lv1"},
        headers=_auth(token),
    )
    assert r.status_code == 201, f"建 lv2 失败: {r.text}"

    return "root", "root/lv1", "root/lv1/lv2"


@pytest.fixture
def seed_root_with_project(client_admin, session_maker):
    """预建根 group 'root'，并直接在 DB 里插一条关联该 group 的 Project。

    这个 fixture 用于"有工程时不能删 group"场景。

    Args:
        client_admin: (client, admin_token) tuple
        session_maker: in-memory SQLite session 工厂，用于直接写 DB

    Returns:
        str：group id（'root'）
    """
    client, token = client_admin

    # admin 建根 group
    r = client.post(
        "/groups",
        json={"id": "root", "name": "根组"},
        headers=_auth(token),
    )
    assert r.status_code == 201, f"建根 group 失败: {r.text}"

    # 直接在 DB 里插一条关联 root group 的 Project
    import asyncio

    async def _add_project():
        async with session_maker() as s:
            s.add(Project(
                id="proj-001",
                name="测试工程",
                group_id="root",     # 关联到 root group
                created_by="admin",  # 必填字段（参考 db_models_homepage.py）
            ))
            await s.commit()

    asyncio.get_event_loop().run_until_complete(_add_project())
    return "root"


@pytest.fixture
def seed_clean_root(client_admin):
    """预建干净的根 group 'root-clean'（无子组、无工程），admin 是 owner。

    这个 fixture 用于"干净时 owner 能删"场景。

    Args:
        client_admin: (client, admin_token) tuple

    Returns:
        tuple：(client, admin_token, group_id)
    """
    client, token = client_admin

    r = client.post(
        "/groups",
        json={"id": "root-clean", "name": "干净根组"},
        headers=_auth(token),
    )
    assert r.status_code == 201, f"建根 group 失败: {r.text}"

    return client, token, "root-clean"


@pytest.fixture
def seed_group_with_members(client_admin, session_maker):
    """预建 'ops' group，admin 是 owner，bob 是 reporter，alice 是 maintainer。

    这个 fixture 用于"maintainer 能改 / reporter 不能改"场景。

    Args:
        client_admin: (client, admin_token) tuple
        session_maker: in-memory SQLite session 工厂

    Returns:
        tuple：(client, admin_token, alice_token, bob_token, group_id)
    """
    client, admin_token = client_admin
    alice_token = _login(client, "alice")
    bob_token = _login(client, "bob")

    # admin 建 group
    r = client.post(
        "/groups",
        json={"id": "ops", "name": "运维组"},
        headers=_auth(admin_token),
    )
    assert r.status_code == 201, f"建 group 失败: {r.text}"

    # 在 DB 里给 alice 加 maintainer、bob 加 reporter
    import asyncio

    async def _add_members():
        from sqlalchemy import select
        from src.service.auth_models import User as UserModel
        async with session_maker() as s:
            alice = await s.scalar(select(UserModel).filter_by(username="alice"))
            bob = await s.scalar(select(UserModel).filter_by(username="bob"))
            s.add_all([
                GroupMember(
                    user_id=alice.id, group_id="ops",
                    role="maintainer", added_by_user_id=alice.id,
                ),
                GroupMember(
                    user_id=bob.id, group_id="ops",
                    role="reporter", added_by_user_id=alice.id,
                ),
            ])
            await s.commit()

    asyncio.get_event_loop().run_until_complete(_add_members())
    return client, admin_token, alice_token, bob_token, "ops"


# ═══════════════════════════════════════════════════════════════════════════════
# 场景测试
# ═══════════════════════════════════════════════════════════════════════════════

# ─── 场景 1：admin 能建根 group ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_can_create_root_group(client_admin):
    """POST /groups：Instance Admin 建根 group → 201，响应包含正确 id。"""
    client, token = client_admin

    # admin POST 建根 group（parent_group_id 不传 = None = 根 group）
    r = client.post(
        "/groups",
        json={
            "id": "retail-bank",        # 业务可读 ID
            "name": "零售银行",
            "description": "零售业务相关工程",
        },
        headers=_auth(token),
    )
    # 断言 201 Created
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] == "retail-bank"  # 返回 id 一致
    assert body["name"] == "零售银行"


# ─── 场景 2：非 admin 不能建根 group ──────────────────────────────────────────

def test_non_admin_cannot_create_root_group(client_bob):
    """POST /groups：非 admin 建根 group → 403 Forbidden。"""
    client, token = client_bob

    r = client.post(
        "/groups",
        json={"id": "unauthorized-root", "name": "无权根组"},
        headers=_auth(token),
    )
    # bob 不是 admin，应返回 403
    assert r.status_code == 403, r.text


# ─── 场景 3：group owner 能建子组 ────────────────────────────────────────────

def test_group_owner_can_create_subgroup(client_alice, seed_root_group_alice_owner):
    """POST /groups：在 parent_group_id=retail-bank 下，alice（owner）能建子组 → 201。"""
    client, alice_token = client_alice

    r = client.post(
        "/groups",
        json={
            "id": "retail-bank/credit-card",
            "name": "信用卡",
            "parent_group_id": "retail-bank",  # 父组 ID
        },
        headers=_auth(alice_token),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] == "retail-bank/credit-card"
    # 确认 parent_group_id 正确保存
    assert body["parent_group_id"] == "retail-bank"


# ─── 场景 4：嵌套深度超过 3 层 → 422 ─────────────────────────────────────────

def test_group_nesting_exceeds_3_layers_rejected(client_admin, seed_3_level_groups):
    """POST /groups：已有 root/lv1/lv2（3 层），再建 lv2 的子组 → 422，错误包含"嵌套深度"。"""
    client, token = client_admin
    # seed_3_level_groups 已建 root → root/lv1 → root/lv1/lv2（第 3 层）
    # 尝试在 lv2 下再建子组 = 第 4 层，应被拒绝
    r = client.post(
        "/groups",
        json={
            "id": "root/lv1/lv2/lv3",
            "name": "lv3（第 4 层）",
            "parent_group_id": "root/lv1/lv2",  # 父是第 3 层，新建 = 第 4 层
        },
        headers=_auth(token),
    )
    assert r.status_code == 422, r.text
    # 错误信息里要有"嵌套深度"关键词（方便前端展示）
    assert "嵌套深度" in r.json()["detail"], r.json()


# ─── 场景 5：父组不存在 → 404 ─────────────────────────────────────────────────

def test_create_with_nonexistent_parent_returns_404(client_admin):
    """POST /groups：parent_group_id 指向不存在的 group → 404。"""
    client, token = client_admin

    r = client.post(
        "/groups",
        json={
            "id": "orphan-child",
            "name": "孤儿子组",
            "parent_group_id": "nonexistent-parent",  # 不存在的父组
        },
        headers=_auth(token),
    )
    assert r.status_code == 404, r.text


# ─── 场景 6：list 可见 groups ──────────────────────────────────────────────────

def test_list_visible_groups(client_admin, client_bob, session_maker):
    """GET /groups：admin 能看到所有 group；bob 只能看到自己有成员关系的 group。"""
    client, admin_token = client_admin
    _, bob_token = client_bob

    # admin 建两个根 group
    for gid, name in [("group-a", "A 组"), ("group-b", "B 组")]:
        r = client.post(
            "/groups",
            json={"id": gid, "name": name},
            headers=_auth(admin_token),
        )
        assert r.status_code == 201, f"建 {gid} 失败: {r.text}"

    # admin 能看到所有 group（is_admin=True → 全可见）
    r = client.get("/groups", headers=_auth(admin_token))
    assert r.status_code == 200, r.text
    ids = [g["id"] for g in r.json()]
    # admin 建了两个 group，都应该在列表里
    assert "group-a" in ids
    assert "group-b" in ids

    # bob 不是任何 group 的成员 → 返回空列表
    r = client.get("/groups", headers=_auth(bob_token))
    assert r.status_code == 200, r.text
    assert r.json() == [], f"bob 应看到空列表，实际: {r.json()}"


# ─── 场景 7：maintainer 能改 name ──────────────────────────────────────────────

def test_maintainer_can_patch_group(seed_group_with_members):
    """PATCH /groups/{gid}：maintainer（alice）能改 name → 200。"""
    client, _, alice_token, _, gid = seed_group_with_members

    r = client.patch(
        f"/groups/{gid}",
        json={"name": "运维组（已更名）"},
        headers=_auth(alice_token),
    )
    assert r.status_code == 200, r.text
    # 确认更名成功
    assert r.json()["name"] == "运维组（已更名）"


# ─── 场景 8：reporter 不能改 → 403 ────────────────────────────────────────────

def test_reporter_cannot_patch_group(seed_group_with_members):
    """PATCH /groups/{gid}：reporter（bob）没有 maintainer+ 权限 → 403。"""
    client, _, _, bob_token, gid = seed_group_with_members

    r = client.patch(
        f"/groups/{gid}",
        json={"name": "试图改名"},
        headers=_auth(bob_token),
    )
    assert r.status_code == 403, r.text


# ─── 场景 9：有子组时不能删 → 403 ────────────────────────────────────────────

def test_cannot_delete_group_with_subgroups(client_admin, seed_2_level_groups):
    """DELETE /groups/{gid}：有子组时删父组 → 403，错误包含"先删子组"。"""
    client, token = client_admin
    root_id, _ = seed_2_level_groups

    # 尝试删有子组的父组
    r = client.delete(f"/groups/{root_id}", headers=_auth(token))
    assert r.status_code == 403, r.text
    assert "先删子组" in r.json()["detail"], r.json()


# ─── 场景 10：有工程时不能删 → 403 ──────────────────────────────────────────

def test_cannot_delete_group_with_projects(client_admin, seed_root_with_project):
    """DELETE /groups/{gid}：有关联工程时删 group → 403，错误包含"先迁移工程"。"""
    client, token = client_admin
    gid = seed_root_with_project

    r = client.delete(f"/groups/{gid}", headers=_auth(token))
    assert r.status_code == 403, r.text
    assert "先迁移工程" in r.json()["detail"], r.json()


# ─── 场景 11：干净 group owner 能删 → 204 ───────────────────────────────────

def test_owner_can_delete_clean_group(seed_clean_root):
    """DELETE /groups/{gid}：干净 group（无子组、无工程）owner 能删 → 204。"""
    client, token, gid = seed_clean_root

    r = client.delete(f"/groups/{gid}", headers=_auth(token))
    assert r.status_code == 204, r.text

    # 确认已删除：再 GET 应该 404
    r2 = client.get(f"/groups/{gid}", headers=_auth(token))
    assert r2.status_code == 404, f"删除后 GET 应 404，实际: {r2.status_code}"


# ─── 额外场景：重复 ID 返回 409 ──────────────────────────────────────────────

def test_duplicate_group_id_returns_409(client_admin):
    """POST /groups：建同 id 的 group 第二次 → 409 Conflict。"""
    client, token = client_admin

    # 第一次建成功
    r = client.post(
        "/groups",
        json={"id": "dup-test", "name": "第一次"},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text

    # 第二次同 id → 409
    r2 = client.post(
        "/groups",
        json={"id": "dup-test", "name": "第二次"},
        headers=_auth(token),
    )
    assert r2.status_code == 409, r2.text
