import pytest, pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select
from src.service.db import Base
from src.service.auth_models import User
from src.service.db_models_homepage import UserScmToken, OAuthState


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s


@pytest.mark.asyncio
async def test_user_identity_columns(session):
    u = User(email="a@x.com", username="alice", hashed_password="h",
             github_user_id=12345, gitlab_sub="sub-1")
    session.add(u); await session.commit()
    row = (await session.execute(select(User).where(User.username == "alice"))).scalar_one()
    assert row.github_user_id == 12345 and row.gitlab_sub == "sub-1"


@pytest.mark.asyncio
async def test_user_identity_defaults_none(session):
    u = User(email="b@x.com", username="bob", hashed_password="h")
    session.add(u); await session.commit()
    row = (await session.execute(select(User).where(User.username == "bob"))).scalar_one()
    assert row.github_user_id is None and row.gitlab_sub is None


@pytest.mark.asyncio
async def test_user_scm_token_and_oauth_state(session):
    u = User(email="c@x.com", username="carol", hashed_password="h")
    session.add(u); await session.commit()
    t = UserScmToken(id="t1", user_id=u.id, provider="github", access_token="enc",
                     scm_login="carol-gh", linked_at=__import__("datetime").datetime(2026, 6, 21))
    s = OAuthState(state_hash="h1", csrf_hash="c1", provider="github", purpose="login",
                   expires_at=__import__("datetime").datetime(2026, 6, 21))
    session.add_all([t, s]); await session.commit()
    assert (await session.execute(select(UserScmToken).where(UserScmToken.id == "t1"))).scalar_one().provider == "github"
    assert (await session.execute(select(OAuthState).where(OAuthState.state_hash == "h1"))).scalar_one().purpose == "login"
