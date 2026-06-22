"""KE-RBAC 集成测试：bind / reindex / index-status 路由的权限门控。

验证：
  - alice（admin）→ bind 200（admin 自动满足 maintainer）
  - bob（非成员普通用户）→ bind/reindex/index-status 全部 403

照搬 test_code_router_resolve.py 的 session_maker + client + _login 模式。
"""
import pytest                   # 测试框架
import pytest_asyncio            # async fixture 支持

from fastapi import FastAPI      # 构造测试用 app
from fastapi.testclient import TestClient  # 同步 HTTP 测试客户端

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# 鉴权相关
from src.service import auth_security as sec   # hash_password 工具函数
from src.service.auth_models import User       # 用户 ORM 模型
from src.service.auth_router import router as auth_router  # /auth/login 等路由

# DB
from src.service.db import Base, get_db        # Base（建表用）+ get_db（dependency 覆盖）
from src.service.db_models_homepage import Project  # 工程 ORM 模型

# 被测路由（使用默认 require_role=require_project_role，不注入 no-op）
from src.service.scm_binding_router import create_scm_binding_routes

# get_current_user：与 api.py 使用的同一个 dependency；
# require_project_role 内部 Depends(get_current_user) 引用的也是这个
from src.service.auth_dependencies import get_current_user


# ─── 内存 DB fixture（seed alice=admin / bob=普通） ───────────────────────────

@pytest_asyncio.fixture
async def session_maker(monkeypatch):
    """内存 SQLite + seed alice(admin) + bob(普通用户) + 工程 p1。

    模仿 test_code_router_resolve.py 的同名 fixture。
    """
    # monkeypatch.setenv：在本次测试期间临时设置环境变量，测试结束后自动恢复
    monkeypatch.setenv("KE_JWT_SECRET", "x" * 32)   # JWT 签名密钥（测试专用）
    monkeypatch.setenv("KE_COOKIE_SECURE", "false")  # 关掉 HTTPS-only cookie 检查

    # 创建内存 SQLite 异步引擎
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    # async with eng.begin() as conn：启动一个事务上下文管理器
    # conn.run_sync(Base.metadata.create_all)：在同步上下文里建表（DDL 通常不支持异步）
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # async_sessionmaker：SQLAlchemy 2.0 的异步 session 工厂
    # expire_on_commit=False：提交后 ORM 对象不过期，避免 DetachedInstanceError
    SM = async_sessionmaker(eng, expire_on_commit=False)

    # seed 数据
    async with SM() as s:
        # alice：is_admin=True → require_project_role 内部 admin 捷径自动通过任何 min_role
        s.add(User(
            email="alice@x.com", username="alice",
            hashed_password=sec.hash_password("12345678"),
            is_active=True, is_admin=True,
        ))
        # bob：is_admin=False，且不在任何工程成员表 → 访问任何工程都 403
        s.add(User(
            email="bob@x.com", username="bob",
            hashed_password=sec.hash_password("12345678"),
            is_active=True, is_admin=False,
        ))
        # 工程 p1：供路由的 404 检查通过（工程存在，才能到达 RBAC 检查）
        s.add(Project(id="p1", name="P1"))
        await s.commit()

    return SM  # 返回 session 工厂，供 client fixture 使用


# ─── TestClient fixture ───────────────────────────────────────────────────────

@pytest.fixture
def client(session_maker):
    """构造测试 app：auth_router + binding 路由（真实 require_role），注入 DB override。

    注意：这里用 create_scm_binding_routes 的默认 require_role（即真实 require_project_role）。
    """
    app = FastAPI()
    app.include_router(auth_router)  # 提供 /auth/login 端点，用于获取 JWT

    # 挂载绑定路由，不传 require_role → 使用默认值 require_project_role（真实 RBAC）
    app.include_router(create_scm_binding_routes(
        get_current_user=get_current_user,  # 与 api.py 一致，从 JWT 解析用户
        get_db=get_db,                      # 使用 get_db dependency（下面会 override）
    ))

    # dependency_overrides：FastAPI 的测试钩子，把 get_db 替换为内存 DB
    # 这样 require_project_role 内部 Depends(get_db) 也会拿到同一个内存会话
    async def override_db():
        """替换 get_db：使用内存 SQLite，保持与 seed 数据库一致。"""
        async with session_maker() as s:
            yield s          # 把 session 交给 FastAPI 依赖系统
            await s.commit() # 测试结束后提交（与 test_code_router_resolve.py 约定一致）

    app.dependency_overrides[get_db] = override_db
    return TestClient(app)  # TestClient 把 async app 包成同步接口，方便测试用 assert


# ─── 辅助函数 ─────────────────────────────────────────────────────────────────

def _login(client: TestClient, username: str = "alice") -> str:
    """登录并返回 JWT access_token。

    直接照搬 test_code_router_resolve.py 的 _login 函数，路由路径和请求体与之一致。
    """
    r = client.post("/auth/login", json={
        "username": username,
        "password": "12345678",
        "remember_me": False,
    })
    assert r.status_code == 200, f"login failed: {r.text}"
    return r.json()["access_token"]  # 返回 JWT 字符串


def _auth(tok: str) -> dict:
    """构造 Bearer Authorization header 字典。"""
    return {"Authorization": f"Bearer {tok}"}


# 有效的 bind 请求体（符合 BindRequest Pydantic 模型）
_BODY = {
    "connection_id": "c1",
    "repo_external_id": 42,
    "repo_full_name": "o/r",
    "ref": "master",
    "ref_type": "branch",
    "subpath": None,
}


# ─── 测试用例 ─────────────────────────────────────────────────────────────────

def test_admin_can_bind(client):
    """alice（admin）→ bind 返回 200。

    admin 在 require_project_role 内部走 is_admin=True → 返回 'owner' 捷径，
    owner ≥ maintainer，所以直接放行。
    """
    tok = _login(client, "alice")
    r = client.post("/projects/p1/bind", json=_BODY, headers=_auth(tok))
    assert r.status_code == 200, r.text


def test_non_member_bind_forbidden(client):
    """bob（无成员关系）→ bind 返回 403。

    bob 不是 admin 也不在 p1 的成员表，resolve_role 返 None → 403。
    """
    tok = _login(client, "bob")
    assert client.post("/projects/p1/bind", json=_BODY, headers=_auth(tok)).status_code == 403


def test_non_member_reindex_forbidden(client):
    """bob → reindex 返回 403（同 bind，需要 maintainer）。"""
    tok = _login(client, "bob")
    assert client.post("/projects/p1/reindex", headers=_auth(tok)).status_code == 403


def test_non_member_index_status_forbidden(client):
    """bob → index-status 返回 403（需要 reporter，bob 无任何成员关系）。"""
    tok = _login(client, "bob")
    assert client.get("/projects/p1/index-status", headers=_auth(tok)).status_code == 403
