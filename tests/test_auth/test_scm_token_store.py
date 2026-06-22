import pytest, pytest_asyncio
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select
from src.service.db import Base
from src.service.auth_models import User
from src.service.db_models_homepage import UserScmToken
from src.service.scm.scm_token_store import (
    upsert_token, get_valid_scm_token, delete_token, ScmTokenInvalid,
)


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def _seed_user(maker) -> int:
    async with maker() as s:
        u = User(email="a@x.com", username="alice", hashed_password="h")
        s.add(u); await s.commit()
        return u.id


@pytest.mark.asyncio
async def test_upsert_and_decrypt_roundtrip(maker):
    uid = await _seed_user(maker)
    async with maker() as s:
        await upsert_token(s, user_id=uid, provider="github", access_token="plain-AT",
                           refresh_token=None, expires_at=None, scopes="read:user", scm_login="alice-gh")
        await s.commit()
    async with maker() as s:
        row = (await s.execute(select(UserScmToken).where(UserScmToken.user_id == uid))).scalar_one()
        assert row.access_token != "plain-AT"            # 密文落库
        tok = await get_valid_scm_token(s, user_id=uid, provider="github", refresh_fn=None)
        assert tok == "plain-AT"                          # 未过期直接解密


@pytest.mark.asyncio
async def test_upsert_keeps_linked_at(maker):
    uid = await _seed_user(maker)
    async with maker() as s:
        await upsert_token(s, user_id=uid, provider="github", access_token="a1",
                           refresh_token=None, expires_at=None, scopes=None, scm_login="alice-gh")
        await s.commit()
    async with maker() as s:
        first = (await s.execute(select(UserScmToken).where(UserScmToken.user_id == uid))).scalar_one().linked_at
    async with maker() as s:  # 复写
        await upsert_token(s, user_id=uid, provider="github", access_token="a2",
                           refresh_token=None, expires_at=None, scopes=None, scm_login="alice-gh2")
        await s.commit()
    async with maker() as s:
        row = (await s.execute(select(UserScmToken).where(UserScmToken.user_id == uid))).scalar_one()
        assert row.linked_at == first           # linked_at 不 bump
        assert row.scm_login == "alice-gh2"     # scm_login 刷新


@pytest.mark.asyncio
async def test_get_valid_refreshes_when_expired(maker):
    uid = await _seed_user(maker)
    async with maker() as s:
        await upsert_token(s, user_id=uid, provider="github", access_token="old-AT",
                           refresh_token="RT", expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                           scopes=None, scm_login="alice-gh")
        await s.commit()

    async def refresh_fn(refresh_token):
        assert refresh_token == "RT"
        return {"access_token": "new-AT", "refresh_token": "RT2",
                "expires_at": datetime.now(timezone.utc) + timedelta(hours=1)}

    async with maker() as s:
        tok = await get_valid_scm_token(s, user_id=uid, provider="github", refresh_fn=refresh_fn)
        await s.commit()
        assert tok == "new-AT"
    async with maker() as s:
        row = (await s.execute(select(UserScmToken).where(UserScmToken.user_id == uid))).scalar_one()
        assert (await get_valid_scm_token(s, user_id=uid, provider="github", refresh_fn=None)) == "new-AT"
        # rotation：新 refresh_token 已持久化（解密验证）
        from src.service.token_crypto import decrypt_token
        assert decrypt_token(row.refresh_token) == "RT2"


@pytest.mark.asyncio
async def test_get_valid_invalid_grant_clears(maker):
    uid = await _seed_user(maker)
    async with maker() as s:
        await upsert_token(s, user_id=uid, provider="github", access_token="old",
                           refresh_token="RT", expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                           scopes=None, scm_login="x")
        await s.commit()

    async def refresh_fn(refresh_token):
        raise ScmTokenInvalid("invalid_grant")

    async with maker() as s:
        with pytest.raises(ScmTokenInvalid):
            await get_valid_scm_token(s, user_id=uid, provider="github", refresh_fn=refresh_fn)
        await s.commit()
    async with maker() as s:
        assert (await s.execute(select(UserScmToken).where(UserScmToken.user_id == uid))).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_delete_token(maker):
    uid = await _seed_user(maker)
    async with maker() as s:
        await upsert_token(s, user_id=uid, provider="github", access_token="a",
                           refresh_token=None, expires_at=None, scopes=None, scm_login="x")
        await s.commit()
    async with maker() as s:
        await delete_token(s, user_id=uid, provider="github"); await s.commit()
    async with maker() as s:
        assert (await s.execute(select(UserScmToken).where(UserScmToken.user_id == uid))).scalar_one_or_none() is None
