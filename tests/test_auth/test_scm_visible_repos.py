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


_API = "https://api.github.com"


def _gh_provider():
    from src.service.scm.github_app import GitHubAppProvider
    from src.service.scm.config import GitHubAppConfig
    return GitHubAppProvider(GitHubAppConfig(app_id="1", private_key_pem="x", webhook_secret=""))


def _repo(rid, name, perms, default_branch="main", private=True):
    return {"id": rid, "full_name": name, "default_branch": default_branch,
            "private": private, "permissions": perms}


@pytest.mark.asyncio
async def test_gh_visible_maps_roles_and_filters(httpx_mock):
    url = f"{_API}/user/installations/55/repositories?per_page=100&page=1"
    httpx_mock.add_response(url=url, json={"repositories": [
        _repo(1, "o/admin", {"admin": True, "push": True, "pull": True}),
        _repo(2, "o/push", {"admin": False, "maintain": False, "push": True, "pull": True}),
        _repo(3, "o/none", {"admin": False, "maintain": False, "push": False, "triage": False, "pull": False}),
    ]})
    out = await _gh_provider().list_user_visible_repos(user_token="UT", installation_id=55)
    by = {v.repo.full_name: v.role for v in out}
    assert by == {"o/admin": ScmRole.CAN_BIND, "o/push": ScmRole.CAN_QUERY}   # o/none 被滤除
    assert all(req.headers["Authorization"] == "token UT" for req in httpx_mock.get_requests())


@pytest.mark.asyncio
async def test_gh_visible_pagination(httpx_mock):
    page1 = {"repositories": [_repo(i, f"o/r{i}", {"pull": True}) for i in range(100)]}
    page2 = {"repositories": [_repo(100, "o/last", {"admin": True})]}
    httpx_mock.add_response(url=f"{_API}/user/installations/55/repositories?per_page=100&page=1", json=page1)
    httpx_mock.add_response(url=f"{_API}/user/installations/55/repositories?per_page=100&page=2", json=page2)
    out = await _gh_provider().list_user_visible_repos(user_token="UT", installation_id=55)
    assert len(out) == 101 and out[-1].repo.full_name == "o/last"


@pytest.mark.asyncio
async def test_gh_visible_no_install_access_404_returns_empty(httpx_mock):
    httpx_mock.add_response(url=f"{_API}/user/installations/55/repositories?per_page=100&page=1",
                            status_code=404)
    out = await _gh_provider().list_user_visible_repos(user_token="UT", installation_id=55)
    assert out == []


@pytest.mark.asyncio
async def test_gh_visible_rate_limited_raises(httpx_mock):
    httpx_mock.add_response(url=f"{_API}/user/installations/55/repositories?per_page=100&page=1",
                            status_code=403, headers={"x-ratelimit-remaining": "0"})
    with pytest.raises(httpx.HTTPError):
        await _gh_provider().list_user_visible_repos(user_token="UT", installation_id=55)


_ISS = "https://gitlab.example.com"


def _gl_provider():
    from src.service.scm.gitlab_oidc import GitLabOidcProvider
    from src.service.scm.config import GitLabOidcConfig
    return GitLabOidcProvider(GitLabOidcConfig(issuer=_ISS, client_id="c", client_secret="s"))


def _proj(pid, name, perms, visibility="private", default_branch="main"):
    return {"id": pid, "path_with_namespace": name, "default_branch": default_branch,
            "visibility": visibility, "permissions": perms}


@pytest.mark.asyncio
async def test_gl_visible_roles_and_inherited_floor(httpx_mock):
    url = f"{_ISS}/api/v4/projects?membership=true&min_access_level=20&per_page=100&page=1"
    httpx_mock.add_response(url=url, json=[
        _proj(1, "g/maint", {"project_access": {"access_level": 40}, "group_access": None}),
        _proj(2, "g/report", {"project_access": {"access_level": 20}, "group_access": None}),
        _proj(3, "g/groupowner", {"project_access": None, "group_access": {"access_level": 50}}),
        _proj(4, "g/inherited", {"project_access": None, "group_access": None}, visibility="public"),
    ])
    out = await _gl_provider().list_user_visible_repos(user_token="AT")
    by = {v.repo.full_name: v.role for v in out}
    assert by == {
        "g/maint": ScmRole.CAN_BIND, "g/report": ScmRole.CAN_QUERY,
        "g/groupowner": ScmRole.CAN_BIND,
        "g/inherited": ScmRole.CAN_QUERY,        # 双 null（纯继承）→ 入选下限 can_query，不被误滤
    }
    pri = {v.repo.full_name: v.repo.private for v in out}
    assert pri["g/inherited"] is False and pri["g/maint"] is True
    assert all(req.headers["Authorization"] == "Bearer AT" for req in httpx_mock.get_requests())


@pytest.mark.asyncio
async def test_gl_visible_pagination(httpx_mock):
    page1 = [_proj(i, f"g/r{i}", {"project_access": {"access_level": 20}}) for i in range(100)]
    page2 = [_proj(100, "g/last", {"project_access": {"access_level": 40}})]
    httpx_mock.add_response(
        url=f"{_ISS}/api/v4/projects?membership=true&min_access_level=20&per_page=100&page=1", json=page1)
    httpx_mock.add_response(
        url=f"{_ISS}/api/v4/projects?membership=true&min_access_level=20&per_page=100&page=2", json=page2)
    out = await _gl_provider().list_user_visible_repos(user_token="AT")
    assert len(out) == 101 and out[-1].repo.full_name == "g/last" and out[-1].role == ScmRole.CAN_BIND
