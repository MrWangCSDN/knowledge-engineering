"""验证 get_current_user 各种 token 状态。"""
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.service import auth_security as sec
from src.service.auth_dependencies import get_current_user
from src.service.auth_models import User
from src.service.db import Base


@pytest_asyncio.fixture
async def db_session(monkeypatch):
    """in-memory sqlite，建表 + 灌一个 user。"""
    monkeypatch.setenv("KE_JWT_SECRET", "x" * 32)
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SM = async_sessionmaker(eng, expire_on_commit=False)
    async with SM() as s:
        u = User(
            email="a@b.com", username="alice",
            hashed_password=sec.hash_password("12345678"),
            is_active=True, is_admin=False,
        )
        s.add(u)
        await s.commit()
    async with SM() as s:
        yield s


@pytest.mark.asyncio
async def test_no_token_raises_401(db_session):
    with pytest.raises(HTTPException) as exc:
        await get_current_user(token=None, db=db_session)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_invalid_token_raises_401(db_session):
    with pytest.raises(HTTPException) as exc:
        await get_current_user(token="garbage", db=db_session)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_refresh_token_rejected(db_session):
    """refresh token 不能当 access token 用。"""
    refresh = sec.create_refresh_token(user_id=1, remember_me=False)
    with pytest.raises(HTTPException) as exc:
        await get_current_user(token=refresh, db=db_session)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_valid_access_token_returns_user(db_session):
    access = sec.create_access_token(user_id=1, username="alice")
    user = await get_current_user(token=access, db=db_session)
    assert user.username == "alice"


@pytest.mark.asyncio
async def test_inactive_user_rejected(db_session):
    """is_active=False 的用户也算 401（不暴露 active 状态）。"""
    from sqlalchemy import update
    await db_session.execute(update(User).where(User.id == 1).values(is_active=False))
    await db_session.commit()
    access = sec.create_access_token(user_id=1, username="alice")
    with pytest.raises(HTTPException) as exc:
        await get_current_user(token=access, db=db_session)
    assert exc.value.status_code == 401
