"""P4c 可见仓列表：VisibleRepo / resolve_caller_token / provider 列仓 / 端点矩阵。"""
import pytest
import pytest_asyncio
import httpx
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from src.service.db_models_homepage import Base
from src.service.scm.base import VisibleRepo, RepoInfo, ScmRole
from src.service.scm.scm_authz import resolve_caller_token
from src.service.scm.scm_token_store import upsert_token, ScmTokenInvalid
from src.service.scm.oauth_factory import OAuthProviderUnavailable


class _User:
    id = 1; username = "alice"; gitlab_sub = None


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


def test_visible_repo_dataclass():
    v = VisibleRepo(repo=RepoInfo(external_id=1, full_name="o/r", default_branch="main"), role=ScmRole.CAN_BIND)
    assert v.repo.external_id == 1 and v.role == ScmRole.CAN_BIND


@pytest.mark.asyncio
async def test_resolve_caller_token_happy(maker):
    async with maker() as s:
        await upsert_token(s, user_id=1, provider="github", access_token="AT",
                           refresh_token=None, expires_at=None, scopes=None, scm_login="alice")
        await s.commit()
    sentinel = object()
    async with maker() as s:
        prov, token = await resolve_caller_token(
            s, user=_User(), provider="github", oauth_cfg=object(),
            get_login_provider=lambda p, cfg: sentinel)
    assert prov is sentinel and token == "AT"


@pytest.mark.asyncio
async def test_resolve_caller_token_provider_unavailable_503(maker):
    def _raise(p, cfg):
        raise OAuthProviderUnavailable("x")
    async with maker() as s:
        with pytest.raises(HTTPException) as ei:
            await resolve_caller_token(s, user=_User(), provider="github",
                                       oauth_cfg=object(), get_login_provider=_raise)
    assert ei.value.status_code == 503


@pytest.mark.asyncio
async def test_resolve_caller_token_no_token_403(maker):
    async with maker() as s:
        with pytest.raises(HTTPException) as ei:
            await resolve_caller_token(s, user=_User(), provider="github",
                                       oauth_cfg=object(), get_login_provider=lambda p, cfg: object())
    assert ei.value.status_code == 403
