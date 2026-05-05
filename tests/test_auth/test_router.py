"""端到端测试 4 个路由（FastAPI TestClient）。"""
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.service import auth_security as sec
from src.service.auth_models import User
from src.service.auth_router import router as auth_router
from src.service.db import Base, get_db


@pytest_asyncio.fixture
async def session_maker(monkeypatch):
    monkeypatch.setenv("KE_JWT_SECRET", "x" * 32)
    monkeypatch.setenv("KE_COOKIE_SECURE", "false")  # 测试用 http
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SM = async_sessionmaker(eng, expire_on_commit=False)
    async with SM() as s:
        s.add(User(email="a@b.com", username="alice",
                   hashed_password=sec.hash_password("12345678"),
                   is_active=True, is_admin=False))
        await s.commit()
    return SM


@pytest.fixture
def client(session_maker):
    app = FastAPI()
    app.include_router(auth_router)

    async def override_db():
        async with session_maker() as s:
            yield s
            await s.commit()
    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


def test_login_success(client):
    r = client.post("/auth/login", json={
        "username": "alice", "password": "12345678", "remember_me": False
    })
    assert r.status_code == 200
    body = r.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"
    assert "refresh_token" in r.cookies


def test_login_wrong_password(client):
    r = client.post("/auth/login", json={
        "username": "alice", "password": "wrong___", "remember_me": False
    })
    assert r.status_code == 401
    assert "用户名或密码不正确" in r.json()["detail"]


def test_login_user_not_exist(client):
    r = client.post("/auth/login", json={
        "username": "no_such", "password": "12345678", "remember_me": False
    })
    assert r.status_code == 401  # 不区分用户不存在 vs 密码错


def test_login_lockout(client):
    """5 次失败后账号锁定。"""
    for _ in range(5):
        client.post("/auth/login", json={
            "username": "alice", "password": "wrong___", "remember_me": False
        })
    r = client.post("/auth/login", json={
        "username": "alice", "password": "12345678", "remember_me": False
    })
    assert r.status_code == 423


def test_me_with_token(client):
    r = client.post("/auth/login", json={
        "username": "alice", "password": "12345678", "remember_me": False
    })
    access = r.json()["access_token"]

    r2 = client.get("/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert r2.status_code == 200
    body = r2.json()
    assert body["username"] == "alice"
    assert body["email"] == "a@b.com"


def test_logout_clears_cookie(client):
    r = client.post("/auth/login", json={
        "username": "alice", "password": "12345678", "remember_me": False
    })
    assert "refresh_token" in r.cookies

    r2 = client.post("/auth/logout")
    assert r2.status_code == 200
    # delete_cookie 通过 Set-Cookie: max-age=0 实现
    set_cookie = r2.headers.get("set-cookie", "")
    assert "refresh_token=" in set_cookie
