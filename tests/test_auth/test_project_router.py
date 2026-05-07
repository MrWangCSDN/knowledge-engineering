"""端到端测试 /projects 路由（FastAPI TestClient + 内存 SQLite）。

跟 test_router.py 同款 pattern：每个测试用临时 in-memory DB，
seed 一个 admin 用户用来做带授权的请求。
"""
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.service import auth_security as sec
from src.service.auth_models import User
from src.service.auth_router import router as auth_router
from src.service.db import Base, get_db
from src.service.db_models_homepage import Project as ProjectModel
from src.service.project_router import router as project_router


# ───────── fixtures ─────────

@pytest_asyncio.fixture
async def session_maker(monkeypatch):
    monkeypatch.setenv("KE_JWT_SECRET", "x" * 32)
    monkeypatch.setenv("KE_COOKIE_SECURE", "false")
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SM = async_sessionmaker(eng, expire_on_commit=False)
    # seed 1 个 admin + 1 个普通用户
    async with SM() as s:
        s.add(User(email="admin@x.com", username="admin",
                   hashed_password=sec.hash_password("12345678"),
                   is_active=True, is_admin=True))
        s.add(User(email="bob@x.com", username="bob",
                   hashed_password=sec.hash_password("12345678"),
                   is_active=True, is_admin=False))
        await s.commit()
    return SM


@pytest.fixture
def client(session_maker):
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(project_router)

    async def override_db():
        async with session_maker() as s:
            yield s
            await s.commit()
    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


def _login_as(client: TestClient, username: str) -> str:
    """登录并返回 access_token（用 12345678 这个 seed 密码）。"""
    r = client.post("/auth/login", json={
        "username": username, "password": "12345678", "remember_me": False
    })
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def seed_project(session_maker):
    """seed 一个 ready 状态的工程供后续测试用。"""
    async with session_maker() as s:
        s.add(ProjectModel(
            id="deposit-system",
            name="存款系统",
            status="ready",
            indexing_progress={
                "methods_count": 100,
                "classes_count": 20,
                "interpretation_progress": 92,
            },
        ))
        await s.commit()
    return "deposit-system"


# ───────── GET /projects ─────────

def test_list_projects_empty(client):
    """无工程 → projects: []。"""
    token = _login_as(client, "admin")
    r = client.get("/projects", headers=_auth(token))
    assert r.status_code == 200
    assert r.json() == {"projects": []}


def test_list_projects_returns_data(client, seed_project):
    token = _login_as(client, "admin")
    r = client.get("/projects", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert len(body["projects"]) == 1
    p = body["projects"][0]
    assert p["id"] == "deposit-system"
    assert p["name"] == "存款系统"
    assert p["status"] == "ready"
    assert p["stats"]["methods_count"] == 100
    assert p["stats"]["interpretation_progress"] == 92


def test_list_projects_requires_auth(client):
    """无 token → 401。"""
    r = client.get("/projects")
    assert r.status_code == 401


# ───────── GET /projects/{id} ─────────

def test_get_project_existing(client, seed_project):
    token = _login_as(client, "admin")
    r = client.get(f"/projects/{seed_project}", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["id"] == seed_project


def test_get_project_404(client):
    token = _login_as(client, "admin")
    r = client.get("/projects/no-such", headers=_auth(token))
    assert r.status_code == 404


# ───────── POST /projects ─────────

def test_create_project_admin_success(client):
    token = _login_as(client, "admin")
    r = client.post("/projects", headers=_auth(token), json={
        "id": "loan-system",
        "name": "贷款系统",
        "repo_url": "git@github.com:org/loan.git",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] == "loan-system"
    assert body["status"] == "indexing"  # 创建后默认 indexing


def test_create_project_non_admin_forbidden(client):
    """普通用户不能创建。"""
    token = _login_as(client, "bob")
    r = client.post("/projects", headers=_auth(token), json={
        "id": "loan-system",
        "name": "贷款系统",
    })
    assert r.status_code == 403


def test_create_project_duplicate_id(client, seed_project):
    """重名 → 409。"""
    token = _login_as(client, "admin")
    r = client.post("/projects", headers=_auth(token), json={
        "id": seed_project,  # 已存在
        "name": "x",
    })
    assert r.status_code == 409


def test_create_project_invalid_id_pattern(client):
    """非法 id (含大写) → 422 (Pydantic 校验)。"""
    token = _login_as(client, "admin")
    r = client.post("/projects", headers=_auth(token), json={
        "id": "DepositSystem",  # 大写不允许
        "name": "x",
    })
    assert r.status_code == 422


def test_create_project_requires_auth(client):
    r = client.post("/projects", json={"id": "x", "name": "x"})
    assert r.status_code == 401
