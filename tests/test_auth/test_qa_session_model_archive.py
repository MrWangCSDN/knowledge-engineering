"""验证 QASession.archived_at 字段存在且默认 None。"""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.service import auth_security as sec
from src.service.auth_models import User
from src.service.db import Base
from src.service.db_models_homepage import Project, QASession


@pytest_asyncio.fixture
async def session_maker():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(eng, expire_on_commit=False)


@pytest.mark.asyncio
async def test_qa_session_has_archived_at_field_default_none(session_maker):
    """新建 QASession 后 archived_at 应该是 None（默认活动状态）。"""
    async with session_maker() as s:
        s.add(User(id=1, email="a@x.com", username="alice",
                   hashed_password=sec.hash_password("12345678"),
                   is_active=True))
        s.add(Project(id="p1", name="P1", status="ready"))
        s.add(QASession(id="sess_1", project_id="p1", user_id=1, title="t"))
        await s.commit()

        sess = await s.get(QASession, "sess_1")
        assert sess is not None
        # archived_at 字段必须存在，且默认 None
        assert sess.archived_at is None


@pytest.mark.asyncio
async def test_qa_session_archived_at_settable(session_maker):
    """archived_at 可以被赋值为 datetime。"""
    from datetime import datetime
    async with session_maker() as s:
        s.add(User(id=1, email="a@x.com", username="alice",
                   hashed_password=sec.hash_password("12345678"),
                   is_active=True))
        s.add(Project(id="p1", name="P1", status="ready"))
        sess = QASession(id="sess_1", project_id="p1", user_id=1, title="t")
        s.add(sess)
        await s.commit()

        sess.archived_at = datetime(2026, 5, 13, 10, 0, 0)
        await s.commit()

        refreshed = await s.get(QASession, "sess_1")
        assert refreshed.archived_at is not None
        assert refreshed.archived_at.year == 2026
