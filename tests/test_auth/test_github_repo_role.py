# tests/test_auth/test_github_repo_role.py
"""GitHubAppProvider resolve_repo_role / list_user_installations 的单元测试（pytest-httpx mock）。
对应 Task 2：user token 状态矩阵 + 只读回退 + 分页。"""
import pytest
from src.service.scm.github_app import GitHubAppProvider
from src.service.scm.config import GitHubAppConfig
from src.service.scm.base import ScmRole

# GitHub REST API 基础 URL，与实现中的 _API 保持一致
_API = "https://api.github.com"


def _provider():
    """构造一个最小配置的 GitHubAppProvider 实例（private_key_pem 填占位符，不涉及 JWT 签名）。"""
    return GitHubAppProvider(GitHubAppConfig(app_id="1", private_key_pem="x", webhook_secret=""))


@pytest.mark.asyncio
async def test_admin_can_bind(httpx_mock):
    """admin role_name → CAN_BIND。"""
    httpx_mock.add_response(url=f"{_API}/repos/o/r/collaborators/octocat/permission",
                            json={"role_name": "admin", "permission": "admin"})
    p = _provider()
    assert await p.resolve_repo_role(token="UT", repo="o/r", principal="octocat") == ScmRole.CAN_BIND


@pytest.mark.asyncio
async def test_uses_user_token_not_installation(httpx_mock, monkeypatch):
    # A7：断言用 user token header，且 get_installation_token 从未被调用
    called = {"inst": False}
    async def _boom(self, installation_id):
        called["inst"] = True
        return "INST"
    monkeypatch.setattr(GitHubAppProvider, "get_installation_token", _boom)
    httpx_mock.add_response(url=f"{_API}/repos/o/r/collaborators/u/permission",
                            json={"role_name": "read", "permission": "read"})
    p = _provider()
    role = await p.resolve_repo_role(token="USER_TOKEN_123", repo="o/r", principal="u")
    assert role == ScmRole.CAN_QUERY
    assert called["inst"] is False
    req = httpx_mock.get_requests()[0]
    assert req.headers["Authorization"] == "token USER_TOKEN_123"


@pytest.mark.asyncio
async def test_scm_login_none_not_visible(httpx_mock):
    p = _provider()
    # principal None → 不发请求、直接 NOT_VISIBLE
    assert await p.resolve_repo_role(token="UT", repo="o/r", principal=None) == ScmRole.NOT_VISIBLE
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_401_not_visible_not_502(httpx_mock):
    httpx_mock.add_response(url=f"{_API}/repos/o/r/collaborators/u/permission", status_code=401)
    p = _provider()
    assert await p.resolve_repo_role(token="UT", repo="o/r", principal="u") == ScmRole.NOT_VISIBLE


@pytest.mark.asyncio
async def test_404_then_repo_fallback_not_visible(httpx_mock):
    httpx_mock.add_response(url=f"{_API}/repos/o/r/collaborators/u/permission", status_code=404)
    httpx_mock.add_response(url=f"{_API}/repos/o/r", status_code=404)
    p = _provider()
    assert await p.resolve_repo_role(token="UT", repo="o/r", principal="u") == ScmRole.NOT_VISIBLE


@pytest.mark.asyncio
async def test_readonly_403_fallback_can_query(httpx_mock):
    # permission 端点对只读用户 403 → 回退 GET /repos 200 → CAN_QUERY
    httpx_mock.add_response(url=f"{_API}/repos/o/r/collaborators/u/permission", status_code=403)
    httpx_mock.add_response(url=f"{_API}/repos/o/r", status_code=200, json={"id": 1})
    p = _provider()
    assert await p.resolve_repo_role(token="UT", repo="o/r", principal="u") == ScmRole.CAN_QUERY


@pytest.mark.asyncio
async def test_rate_limited_403_raises(httpx_mock):
    httpx_mock.add_response(url=f"{_API}/repos/o/r/collaborators/u/permission",
                            status_code=403, headers={"x-ratelimit-remaining": "0"}, json={"message": "rate limit"})
    p = _provider()
    with pytest.raises(Exception):   # 限流 → 抛（上层 502）
        await p.resolve_repo_role(token="UT", repo="o/r", principal="u")


@pytest.mark.asyncio
async def test_list_user_installations_paginated(httpx_mock):
    page1 = {"installations": [{"id": i} for i in range(100)]}
    page2 = {"installations": [{"id": 999}]}
    httpx_mock.add_response(url=f"{_API}/user/installations?per_page=100&page=1", json=page1)
    httpx_mock.add_response(url=f"{_API}/user/installations?per_page=100&page=2", json=page2)
    p = _provider()
    ids = await p.list_user_installations(user_token="UT")
    assert 999 in ids and len(ids) == 101
    assert httpx_mock.get_requests()[0].headers["Authorization"] == "token UT"
