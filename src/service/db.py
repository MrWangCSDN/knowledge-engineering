"""SQLAlchemy 2.0 async engine + sessionmaker + FastAPI dependency.

env vars:
  KE_DB_URL  e.g. mysql+asyncmy://user:pwd@host:3306/dbname
"""
from __future__ import annotations

import os
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """SQLAlchemy 2.0 风格的 ORM 基类，所有 model 继承自此。"""


def _build_engine() -> AsyncEngine:
    url = os.getenv("KE_DB_URL", "")
    if not url:
        raise RuntimeError("KE_DB_URL not set; check /opt/knowledge-engineering/.env")
    # SQLite 使用 StaticPool，不支持 pool_size / max_overflow；MySQL 才需要连接池配置
    is_sqlite = url.startswith("sqlite")
    kwargs: dict = dict(
        echo=False,                  # 生产关 SQL log
        pool_pre_ping=not is_sqlite, # 自动检测断连，重连（SQLite 不支持）
        pool_recycle=1800 if not is_sqlite else -1,  # 30 分钟回收（避开 MySQL 默认 8h timeout）
    )
    if not is_sqlite:
        kwargs["pool_size"] = 5
        kwargs["max_overflow"] = 10
    return create_async_engine(url, **kwargs)


_engine: AsyncEngine | None = None
_SessionMaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """单例 engine。"""
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def get_session_maker() -> async_sessionmaker[AsyncSession]:
    global _SessionMaker
    if _SessionMaker is None:
        _SessionMaker = async_sessionmaker(get_engine(), expire_on_commit=False, class_=AsyncSession)
    return _SessionMaker


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency：每个请求一个 session，自动 commit/rollback。"""
    async with get_session_maker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
