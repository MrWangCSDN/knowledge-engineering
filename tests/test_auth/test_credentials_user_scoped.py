"""v2.0 user-scoped credentials 测试。

测试策略：
  - 3 个用户 fixture：
      client_admin        — is_admin=True（同时用于 /admin/credentials 审计视角）
      client_alice_nonadmin — 普通用户 alice
      client_bob          — 普通用户 bob
  - 所有路由通过 FastAPI TestClient 测试（同步包装，无需 async/await）
  - 使用 in-memory SQLite，每个 fixture 独享

测试清单：
  1. 普通用户能创建凭证（v2.0 起不再 admin-only）
  2. 用户只能看到自己的凭证（隔离性）
  3. admin 通过 /admin/credentials 能看到所有凭证（审计视角）
  4. 用户不能删别人的凭证（403）
  5. 用户可以删自己的凭证（204）
  6. 删除不存在的凭证 → 404
  7. 未认证请求 → 401
  8. admin 通过 /admin/credentials 强删任意凭证（无所有权限制）
  9. POST /admin/credentials 已被移除 → 404 或 405
"""
import os  # 标准库：读取环境变量（KE_TOKEN_ENC_KEY / KE_JWT_SECRET）

import pytest              # pytest：断言增强、fixture 注入
import pytest_asyncio      # pytest_asyncio：支持 async fixture（@pytest_asyncio.fixture）

from fastapi import FastAPI  # FastAPI 应用类
from fastapi.testclient import TestClient  # TestClient：同步测试客户端，包装 ASGI app
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # 异步 ORM 引擎+工厂

from src.service import auth_security as sec  # 密码 hash 工具（hash_password）
from src.service.auth_models import User       # ORM 用户模型
from src.service.auth_router import router as auth_router       # 登录路由
from src.service.admin_router import router as admin_router     # admin 凭证路由（/admin/credentials/*）
from src.service.credentials_router import router as credentials_router  # 用户级凭证路由（/credentials/*）
from src.service.db import Base, get_db        # ORM 基类（建表用）+ DB 依赖注入


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures（测试夹具）
# ═══════════════════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def session_maker(monkeypatch):
    """提供一个 in-memory SQLite 的 session 工厂，seed 3 个用户。

    monkeypatch：pytest 内置 fixture，用于临时修改环境变量 / 模块属性，
    测试结束后自动恢复，不影响其他测试。

    KE_TOKEN_ENC_KEY：Fernet 加密密钥（base64，44 字节）；
    测试里用固定的临时密钥，cryptography.fernet.Fernet.generate_key() 格式。

    Args:
        monkeypatch: pytest 内置 fixture，临时修改环境变量。

    Yields:
        async_sessionmaker：可用于创建数据库 session 的工厂。
    """
    # 设置 JWT 密钥（登录 /auth/login 时签名 token 用）
    monkeypatch.setenv("KE_JWT_SECRET", "x" * 32)
    # 关闭 Secure cookie（测试用 http，不是 https）
    monkeypatch.setenv("KE_COOKIE_SECURE", "false")
    # 设置 Fernet 加密密钥（encrypt_token/decrypt_token 用），用合法的 Fernet key 格式
    # Fernet.generate_key() 产出 44 字节 base64；这里用一个固定测试密钥
    monkeypatch.setenv("KE_TOKEN_ENC_KEY", "YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXoxMjM0NTY=")

    # 重置 Fernet 的 lru_cache（否则 monkeypatch 的 env 不生效，因为旧 Fernet 实例已缓存）
    from src.service.token_crypto import reset_fernet_cache
    reset_fernet_cache()

    # 创建纯内存 SQLite 异步引擎（每次 fixture 都是全新数据库，测试完全隔离）
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    # run_sync：在异步上下文里执行同步函数（Base.metadata.create_all 是同步的）
    # Base.metadata.create_all：创建所有继承自 Base 的 ORM 类对应的数据库表
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # async_sessionmaker：异步 session 工厂
    # expire_on_commit=False：commit 后不让 ORM 对象过期（避免访问属性时额外 SELECT）
    SM = async_sessionmaker(eng, expire_on_commit=False)

    # seed 3 个用户：admin（is_admin=True）、alice（普通）、bob（普通）
    async with SM() as s:
        s.add_all([
            User(
                email="admin@x.com", username="admin",
                hashed_password=sec.hash_password("12345678"),
                is_active=True, is_admin=True,  # Instance Admin
            ),
            User(
                email="alice@x.com", username="alice",
                hashed_password=sec.hash_password("12345678"),
                is_active=True, is_admin=False,  # 普通用户
            ),
            User(
                email="bob@x.com", username="bob",
                hashed_password=sec.hash_password("12345678"),
                is_active=True, is_admin=False,  # 普通用户
            ),
        ])
        await s.commit()

    yield SM  # yield 把 SM 注入给依赖此 fixture 的 fixture

    # fixture 结束后自动清理 Fernet 缓存，避免污染其他测试
    reset_fernet_cache()


def _make_app(session_maker) -> FastAPI:
    """构造测试用 FastAPI app，注入 3 个 router + 覆盖 get_db 依赖。

    dependency_overrides：FastAPI 提供的依赖覆盖机制，
    让 get_db 在测试里返回 in-memory SQLite session，而不是真实数据库连接。

    Args:
        session_maker: async_sessionmaker 实例，用于创建测试 DB session。

    Returns:
        FastAPI：装配好路由和依赖覆盖的测试 app。
    """
    app = FastAPI()
    app.include_router(auth_router)          # 登录/登出路由（/auth/*）
    app.include_router(admin_router)         # admin 凭证审计路由（/admin/credentials/*）
    app.include_router(credentials_router)  # 用户级凭证路由（/credentials/*）

    # 覆盖 get_db 依赖：把 in-memory SQLite session 注入给所有路由
    async def override_db():
        # async with SM() as s：异步上下文管理器，自动关闭 session
        async with session_maker() as s:
            yield s       # yield 把 session 注入给路由函数
            await s.commit()  # 路由返回后自动 commit（避免测试 session 里数据丢失）

    # dependency_overrides 是一个字典：{原依赖函数: 覆盖函数}
    app.dependency_overrides[get_db] = override_db
    return app


def _login(client: TestClient, username: str) -> str:
    """辅助函数：登录并返回 JWT access_token。

    Args:
        client: TestClient 实例。
        username: 用户名（seed 密码统一为 '12345678'）。

    Returns:
        str：JWT access_token，用于后续请求的 Authorization header。
    """
    r = client.post("/auth/login", json={
        "username": username,
        "password": "12345678",
        "remember_me": False,
    })
    # assert：断言登录成功；r.text 作为断言失败时的调试信息
    assert r.status_code == 200, r.text
    return r.json()["access_token"]  # 取 access_token 字段


def _auth(token: str) -> dict:
    """辅助函数：生成 Authorization header dict。

    Args:
        token: JWT access_token。

    Returns:
        dict：{"Authorization": "Bearer <token>"}，可直接传给 TestClient 的 headers 参数。
    """
    return {"Authorization": f"Bearer {token}"}


# ─── client fixture（每个测试函数共享同一个 app 实例，通过 session_maker 隔离数据） ─

@pytest.fixture
def client(session_maker):
    """返回一个绑定了 session_maker 的 TestClient。

    TestClient 是 Starlette 提供的同步测试 HTTP 客户端，
    内部使用 threading + asyncio.run，不需要 async/await。

    Args:
        session_maker: in-memory SQLite session 工厂（来自 session_maker fixture）。

    Returns:
        TestClient：可直接发 HTTP 请求的测试客户端。
    """
    return TestClient(_make_app(session_maker))


# ─── 专用 client fixture（返回已登录的 TestClient + token）──────────────────

@pytest.fixture
def client_admin(client):
    """已登录为 admin 的 TestClient。

    Returns:
        tuple[TestClient, str]：(client, admin_token)
    """
    token = _login(client, "admin")
    return client, token


@pytest.fixture
def client_alice(client):
    """已登录为 alice（普通用户）的 TestClient。

    Returns:
        tuple[TestClient, str]：(client, alice_token)
    """
    token = _login(client, "alice")
    return client, token


@pytest.fixture
def client_bob(client):
    """已登录为 bob（普通用户）的 TestClient。

    Returns:
        tuple[TestClient, str]：(client, bob_token)
    """
    token = _login(client, "bob")
    return client, token


# ═══════════════════════════════════════════════════════════════════════════════
# 测试用例
# ═══════════════════════════════════════════════════════════════════════════════

def test_create_credential_owned_by_current_user(client_bob):
    """普通用户（bob）也能建凭证（v2.0 起不再 admin-only）。

    验证：
    1. POST /credentials → 201（建立成功）
    2. 返回 id / name / type / token_hint / created_at
    3. 立刻 GET /credentials 能查到刚建的凭证
    4. token / encrypted_token 永不出现在响应里
    """
    # client_bob 是 (client, token) 元组，用解包语法取出两个变量
    client, token = client_bob

    # 发 POST /credentials 创建凭证
    r = client.post(
        "/credentials",
        json={"name": "bob 的 PAT", "token": "sk-test-fake-1234567"},
        headers=_auth(token),  # Bearer token 认证
    )
    # 断言：201 Created（附上 r.text 方便调试）
    assert r.status_code == 201, r.text

    body = r.json()
    # 凭证 ID 应以 'cred_' 开头
    assert body["id"].startswith("cred_")
    assert body["name"] == "bob 的 PAT"
    # token_hint 是末 4 位掩码，不应包含原始 token
    assert "token" not in body
    assert "encrypted_token" not in body

    # 立刻 GET /credentials 验证能查到
    r2 = client.get("/credentials", headers=_auth(token))
    assert r2.status_code == 200, r2.text

    names = [c["name"] for c in r2.json()]
    # assert ... in names：检查列表里是否存在该名称
    assert "bob 的 PAT" in names


def test_user_cannot_see_others_credentials(client_alice, client_bob):
    """bob 看不到 alice 创建的凭证（user-scoped 隔离性）。

    设计验证：每个用户只能看到 owner_user_id == 自己 id 的凭证。
    alice 创建的凭证 owner_user_id = alice.id，bob 查 /credentials 时
    会 filter_by(owner_user_id=bob.id)，所以看不到 alice 的凭证。
    """
    client, alice_token = client_alice
    _, bob_token = client_bob

    # alice 先建一条凭证
    r = client.post(
        "/credentials",
        json={"name": "alice PAT", "token": "sk-a-xxx-12345678"},
        headers=_auth(alice_token),
    )
    assert r.status_code == 201, r.text

    # bob 查自己的凭证列表
    r2 = client.get("/credentials", headers=_auth(bob_token))
    assert r2.status_code == 200, r2.text

    # alice 的凭证不应出现在 bob 的列表里
    names = [c["name"] for c in r2.json()]
    assert "alice PAT" not in names


def test_admin_sees_all_via_admin_endpoint(client_admin, client_bob):
    """Instance Admin 通过 /admin/credentials 能看到所有用户的凭证（审计视角）。

    验证：
    1. bob 创建凭证
    2. admin 通过 GET /admin/credentials 能看到 bob 的凭证
    3. 响应体里不含 token / encrypted_token（只有 hint）
    """
    client, admin_token = client_admin
    _, bob_token = client_bob

    # bob 先建一条凭证
    r = client.post(
        "/credentials",
        json={"name": "bob PAT", "token": "sk-b-yyy-67890123"},
        headers=_auth(bob_token),
    )
    assert r.status_code == 201, r.text

    # admin 通过 /admin/credentials 查全部（审计接口）
    r2 = client.get("/admin/credentials", headers=_auth(admin_token))
    assert r2.status_code == 200, r2.text

    creds = r2.json()["credentials"]  # CredentialListResponse 的 .credentials 字段

    # 至少能看到 bob 的凭证
    bob_creds = [c for c in creds if c["name"] == "bob PAT"]
    assert len(bob_creds) == 1

    # 安全校验：任何凭证都不应包含原始 token 或密文
    # 使用 for 循环逐一检查（比 all() 失败时错误信息更清晰）
    for c in creds:
        assert "token" not in c, f"凭证 {c['id']} 泄露了 token 字段"
        assert "encrypted_token" not in c, f"凭证 {c['id']} 泄露了 encrypted_token 字段"


def test_user_cannot_delete_others_credentials(client_alice, client_bob):
    """alice 不能删 bob 的凭证 → 403 Forbidden。

    验证所有权校验：DELETE /credentials/{id} 会检查 cred.owner_user_id == current_user.id，
    不是则返回 403（不是 404，防止枚举攻击）。
    """
    client, alice_token = client_alice
    _, bob_token = client_bob

    # bob 先建凭证
    r_create = client.post(
        "/credentials",
        json={"name": "bob private", "token": "sk-bob-xxx-12345678"},
        headers=_auth(bob_token),
    )
    assert r_create.status_code == 201, r_create.text
    # 从响应体取凭证 ID
    cid = r_create.json()["id"]

    # alice 尝试删 bob 的凭证 → 403
    r_delete = client.delete(f"/credentials/{cid}", headers=_auth(alice_token))
    assert r_delete.status_code == 403, r_delete.text


def test_user_can_delete_own_credential(client_bob):
    """bob 能删自己的凭证 → 204 No Content。

    同时验证删除后 GET /credentials 里看不到该凭证了。
    """
    client, token = client_bob

    # 建凭证
    r = client.post(
        "/credentials",
        json={"name": "to be deleted", "token": "sk-del-xxx-12345678"},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    cid = r.json()["id"]

    # 删凭证 → 204 No Content（FastAPI 204 响应体为空）
    r_del = client.delete(f"/credentials/{cid}", headers=_auth(token))
    assert r_del.status_code == 204, r_del.text

    # 验证已从列表中消失
    r_list = client.get("/credentials", headers=_auth(token))
    assert r_list.status_code == 200
    ids = [c["id"] for c in r_list.json()]
    # assert ... not in ids：删除后 ID 不应再出现
    assert cid not in ids


def test_delete_nonexistent_credential_returns_404(client_bob):
    """删除不存在的凭证 → 404 Not Found。"""
    client, token = client_bob

    # 用一个不存在的 ID 发 DELETE 请求
    r = client.delete("/credentials/cred_nonexistent", headers=_auth(token))
    assert r.status_code == 404, r.text


def test_list_credentials_requires_auth(client):
    """未认证请求 GET /credentials → 401 Unauthorized。"""
    # 不传 Authorization header
    r = client.get("/credentials")
    assert r.status_code == 401, r.text


def test_create_credential_requires_auth(client):
    """未认证请求 POST /credentials → 401 Unauthorized。"""
    r = client.post("/credentials", json={"name": "x", "token": "sk-test-12345678"})
    assert r.status_code == 401, r.text


def test_admin_can_force_delete_any_credential(client_admin, client_bob):
    """admin 通过 /admin/credentials/{id} 可以强删任意用户的凭证。

    验证：admin 强删 bob 的凭证后，bob 的凭证列表里看不到该凭证了。
    """
    client, admin_token = client_admin
    _, bob_token = client_bob

    # bob 建凭证
    r = client.post(
        "/credentials",
        json={"name": "bob force-delete", "token": "sk-f-del-12345678"},
        headers=_auth(bob_token),
    )
    assert r.status_code == 201, r.text
    cid = r.json()["id"]

    # admin 强删（不受 owner_user_id 限制）
    r_del = client.delete(f"/admin/credentials/{cid}", headers=_auth(admin_token))
    assert r_del.status_code == 204, r_del.text

    # bob 验证凭证已消失
    r_list = client.get("/credentials", headers=_auth(bob_token))
    assert r_list.status_code == 200
    ids = [c["id"] for c in r_list.json()]
    assert cid not in ids


def test_post_admin_credentials_removed(client_admin):
    """POST /admin/credentials 已被移除（v2.0 创建凭证移到 /credentials）→ 404 或 405。

    这个测试确保旧 admin-only 创建凭证端点不再存在，
    防止未来代码误回退到 v1.0 行为。
    """
    client, token = client_admin

    r = client.post(
        "/admin/credentials",
        json={"name": "should-fail", "token": "sk-test-12345678"},
        headers=_auth(token),
    )
    # 404 Not Found（路由不存在）或 405 Method Not Allowed（路由存在但不支持 POST）
    # in {404, 405}：检查 status_code 是否在这个集合里（Python 集合的 in 操作）
    assert r.status_code in {404, 405}, (
        f"POST /admin/credentials 应该已被移除，但返回了 {r.status_code}: {r.text}"
    )
