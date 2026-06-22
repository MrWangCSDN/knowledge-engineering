from datetime import datetime
import pytest
from src.service.scm.scm_refresh import build_refresh_fn
from src.service.scm.scm_token_store import ScmTokenInvalid
from src.service.scm.config import OAuthConfig, GitHubOAuthConfig, GitLabOidcConfig
from src.service.scm.gitlab_oidc import GitLabOidcProvider

GH_TOKEN_URL = "https://github.com/login/oauth/access_token"
ISS = "https://gitlab.example.com"


def _gh_cfg():
    return OAuthConfig(redirect_base="https://ke", github=GitHubOAuthConfig("cid", "sec"), gitlab=None)


@pytest.mark.asyncio
async def test_github_refresh_success_expires_at_is_datetime(httpx_mock):
    httpx_mock.add_response(url=GH_TOKEN_URL, json={"access_token": "NEW", "refresh_token": "R2", "expires_in": 3600})
    fn = build_refresh_fn("github", oauth_cfg=_gh_cfg())
    out = await fn("R1")
    assert out["access_token"] == "NEW" and out["refresh_token"] == "R2"
    assert isinstance(out["expires_at"], datetime)   # 非 int


@pytest.mark.asyncio
async def test_github_refresh_invalid_grant_raises(httpx_mock):
    # GitHub 返 HTTP 200 + body error
    httpx_mock.add_response(url=GH_TOKEN_URL, json={"error": "bad_refresh_token"})
    fn = build_refresh_fn("github", oauth_cfg=_gh_cfg())
    with pytest.raises(ScmTokenInvalid):
        await fn("R1")


@pytest.mark.asyncio
async def test_github_bad_creds_401_not_token_invalid(httpx_mock):
    # 运维配错 client creds（401）不可误判为 invalid_grant（否则会删用户关联）
    httpx_mock.add_response(url=GH_TOKEN_URL, status_code=401, json={"message": "Bad credentials"})
    fn = build_refresh_fn("github", oauth_cfg=_gh_cfg())
    with pytest.raises(Exception) as ei:
        await fn("R1")
    assert not isinstance(ei.value, ScmTokenInvalid)


def test_github_no_creds_returns_none():
    assert build_refresh_fn("github", oauth_cfg=OAuthConfig(redirect_base="x", github=None, gitlab=None)) is None


@pytest.mark.asyncio
async def test_gitlab_refresh_uses_discovered_endpoint(httpx_mock):
    httpx_mock.add_response(url=f"{ISS}/.well-known/openid-configuration",
                            json={"issuer": ISS, "authorization_endpoint": f"{ISS}/oauth/authorize",
                                  "token_endpoint": f"{ISS}/oauth/token", "jwks_uri": f"{ISS}/keys",
                                  "id_token_signing_alg_values_supported": ["RS256"]})
    httpx_mock.add_response(url=f"{ISS}/oauth/token", json={"access_token": "NEW", "expires_in": 7200})
    prov = GitLabOidcProvider(GitLabOidcConfig(issuer=ISS, client_id="c", client_secret="s"))
    fn = build_refresh_fn("gitlab", gitlab_provider=prov)
    out = await fn("R1")
    assert out["access_token"] == "NEW" and isinstance(out["expires_at"], datetime)


@pytest.mark.asyncio
async def test_gitlab_refresh_invalid_grant_raises(httpx_mock):
    httpx_mock.add_response(url=f"{ISS}/.well-known/openid-configuration",
                            json={"issuer": ISS, "authorization_endpoint": f"{ISS}/a",
                                  "token_endpoint": f"{ISS}/oauth/token", "jwks_uri": f"{ISS}/k",
                                  "id_token_signing_alg_values_supported": ["RS256"]})
    httpx_mock.add_response(url=f"{ISS}/oauth/token", status_code=400, json={"error": "invalid_grant"})
    prov = GitLabOidcProvider(GitLabOidcConfig(issuer=ISS, client_id="c", client_secret="s"))
    fn = build_refresh_fn("gitlab", gitlab_provider=prov)
    with pytest.raises(ScmTokenInvalid):
        await fn("R1")
