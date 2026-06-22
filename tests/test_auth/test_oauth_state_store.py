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


@pytest.mark.asyncio
async def test_concurrent_consume_single_winner(maker):
    """并发双消费：rowcount==1 门确保仅一个调用成功，另一个得到 None（防重放）。

    aiosqlite 使用 in-memory 数据库时连接是串行化的，asyncio.gather 会让两次
    consume_state 顺序执行——仍能验证"第二次消费必为 None"的核心属性。
    """
    import asyncio  # asyncio：Python 异步 I/O 标准库；gather 并发调度多个协程

    # 先 mint 一条 state 并 commit（让下面两个并发消费共享同一行）
    async with maker() as s:
        # with_nonce=False：不需要 OIDC nonce，简化测试
        minted = await mint_state(s, provider="github", purpose="login", user_id=None, with_nonce=False)
        await s.commit()  # commit：写入 DB，让其他 session 可见

    # _try 协程：在独立 session 里尝试消费同一 state，返回 consume_state 结果
    async def _try():
        # 每次 _try 开一个新 session（模拟独立请求），maker() 是 async_sessionmaker 实例
        async with maker() as s:
            # consume_state：尝试原子消费，失败（并发抢先或不存在）返回 None
            r = await consume_state(s, state=minted.state, csrf=minted.csrf)
            await s.commit()  # commit：持久化 DELETE（rowcount 门关闭后）
            return r  # 返回 OAuthState 行或 None

    # asyncio.gather：并发调度两个 _try 协程；aiosqlite in-memory 会串行执行，
    # 但两次请求的输入相同——正好验证"单行只能被消费一次"的不变量
    results = await asyncio.gather(_try(), _try())

    # 筛出成功（非 None）的结果：必须恰好只有一个赢家
    winners = [r for r in results if r is not None]
    # len==1：rowcount 门确保串行或并发场景下只有一个消费者拿到行
    assert len(winners) == 1
