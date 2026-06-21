# tests/test_auth/test_oauth_state_store.py
import pytest, pytest_asyncio
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from src.service.db import Base
from src.service.db_models_homepage import OAuthState
from src.service.scm.oauth_state_store import mint_state, consume_state, gc_expired


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_mint_then_consume_once(maker):
    async with maker() as s:
        minted = await mint_state(s, provider="github", purpose="login", user_id=None, with_nonce=True)
        await s.commit()
    # minted.state / minted.csrf 是返给浏览器的明文；DB 存 hash
    async with maker() as s:
        row = await consume_state(s, state=minted.state, csrf=minted.csrf)
        await s.commit()
    assert row is not None and row.provider == "github" and row.purpose == "login" and row.nonce
    # 第二次消费 → None（已删）
    async with maker() as s:
        assert await consume_state(s, state=minted.state, csrf=minted.csrf) is None


@pytest.mark.asyncio
async def test_consume_bad_csrf(maker):
    async with maker() as s:
        minted = await mint_state(s, provider="github", purpose="login", user_id=None, with_nonce=False)
        await s.commit()
    async with maker() as s:
        assert await consume_state(s, state=minted.state, csrf="wrong") is None  # csrf 不符


@pytest.mark.asyncio
async def test_consume_expired(maker):
    async with maker() as s:
        minted = await mint_state(s, provider="github", purpose="login", user_id=None,
                                  with_nonce=False, ttl_seconds=-1)  # 立刻过期
        await s.commit()
    async with maker() as s:
        assert await consume_state(s, state=minted.state, csrf=minted.csrf) is None


@pytest.mark.asyncio
async def test_gc_expired(maker):
    async with maker() as s:
        await mint_state(s, provider="github", purpose="login", user_id=None, with_nonce=False, ttl_seconds=-1)
        await s.commit()
    async with maker() as s:
        n = await gc_expired(s); await s.commit()
    assert n >= 1
