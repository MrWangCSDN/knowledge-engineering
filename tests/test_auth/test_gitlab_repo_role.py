# tests/test_auth/test_gitlab_repo_role.py
"""GitLabOidcProvider.resolve_repo_role 权限解析测试（Members API /all + sub 守卫 + 状态矩阵）。"""
import pytest
from src.service.scm.gitlab_oidc import GitLabOidcProvider
from src.service.scm.config import GitLabOidcConfig
from src.service.scm.base import ScmRole

ISS = "https://gitlab.example.com"


def _provider():
    # 构造最小配置的 GitLabOidcProvider，用于所有测试
    return GitLabOidcProvider(GitLabOidcConfig(issuer=ISS, client_id="c", client_secret="s"))


@pytest.mark.asyncio
async def test_maintainer_can_bind(httpx_mock):
    # access_level=40（Maintainer）→ CAN_BIND
    httpx_mock.add_response(url=f"{ISS}/api/v4/projects/42/members/all/7", json={"access_level": 40})
    p = _provider()
    assert await p.resolve_repo_role(token="AT", repo=42, principal="7") == ScmRole.CAN_BIND


@pytest.mark.asyncio
async def test_reporter_can_query(httpx_mock):
    # access_level=20（Reporter）→ CAN_QUERY
    httpx_mock.add_response(url=f"{ISS}/api/v4/projects/42/members/all/7", json={"access_level": 20})
    p = _provider()
    assert await p.resolve_repo_role(token="AT", repo=42, principal="7") == ScmRole.CAN_QUERY


@pytest.mark.asyncio
async def test_guest_not_visible(httpx_mock):
    # access_level=10（Guest）< 20 → NOT_VISIBLE（Guest-trap）
    httpx_mock.add_response(url=f"{ISS}/api/v4/projects/42/members/all/7", json={"access_level": 10})
    p = _provider()
    assert await p.resolve_repo_role(token="AT", repo=42, principal="7") == ScmRole.NOT_VISIBLE


@pytest.mark.asyncio
async def test_non_member_404_not_visible(httpx_mock):
    # 404（非成员）→ NOT_VISIBLE
    httpx_mock.add_response(url=f"{ISS}/api/v4/projects/42/members/all/7", status_code=404)
    p = _provider()
    assert await p.resolve_repo_role(token="AT", repo=42, principal="7") == ScmRole.NOT_VISIBLE


@pytest.mark.asyncio
async def test_401_not_visible(httpx_mock):
    # 401（token 无效/过期）→ NOT_VISIBLE
    httpx_mock.add_response(url=f"{ISS}/api/v4/projects/42/members/all/7", status_code=401)
    p = _provider()
    assert await p.resolve_repo_role(token="AT", repo=42, principal="7") == ScmRole.NOT_VISIBLE


@pytest.mark.asyncio
async def test_non_numeric_sub_guarded(httpx_mock):
    # 非数字 sub → 不发请求、NOT_VISIBLE（守卫）
    p = _provider()
    assert await p.resolve_repo_role(token="AT", repo=42, principal="not-a-number") == ScmRole.NOT_VISIBLE
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_uses_bearer(httpx_mock):
    # 验证 Authorization 头为 Bearer <token>
    httpx_mock.add_response(url=f"{ISS}/api/v4/projects/42/members/all/7", json={"access_level": 30})
    p = _provider()
    await p.resolve_repo_role(token="AT_XYZ", repo=42, principal="7")
    assert httpx_mock.get_requests()[0].headers["Authorization"] == "Bearer AT_XYZ"
