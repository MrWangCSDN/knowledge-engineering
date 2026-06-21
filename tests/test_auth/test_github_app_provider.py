"""GitHubAppProvider 测试：JWT→installation token + 缓存（pytest-httpx mock）。"""
import time
import pytest

from src.service.scm.config import GitHubAppConfig
from src.service.scm.github_app import GitHubAppProvider

_TEST_PEM = """-----BEGIN PRIVATE KEY-----
MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQDJ95uHsNe3THcl
J/gcP6JocCStjRNLu0nXcXI+ADsgO1VbMh8jYCqCYOvRkA5RXu2wXiqDGxVY+PDk
JUQE3SsscuTXws/8BqnRe0p9uqrjBlnc+QvERw+u8de7OsiE8elaqfsdfRxRkM6n
o+bRbvT2sZoguhH5BlcsYdPyafuc0lDsjg12Uhfh/+UpiuQDekyKHxHiti7ZBq5u
8RFWgAcs1KghR3rDEr1ysBNQzsLPsEr+tBbvzVJBMy6OEn57PvYkAOIR2mW/PX1v
gMz9Dj41B4vgLtrBLF0ouOQMYMY5MsHTH25eHMhDmfMhhKP2GaM5x/fpmweSHsHc
kZlB/pwdAgMBAAECggEAC9bG4tpb8B5nBuI5P2FU4i2zXgpcrN4gAvq0dAFMIKSP
FAHJJNK4oIc765SNMk86kacYyNyn5geZtKtL5G5uicl7s/tzI6vkUnzureto+avf
9/26py50iJBxUUQDMxUsf6hifvpcKZqFHVCUnDGWjHYhZHUOeVGE1KYwvWS3ChyI
iRZMtwq4F3PMx3ll+tto1ouA4m2rnFvszvICqfCnqzuJJsvQLftqMpWna/mO90LQ
hD6M/ArFKY2lQ81Hkw49A5gzcRpNwStT6dGS18VRCqpbVVbTOceP4voWjNKUDiGJ
2qrLTy6g/J5M28jX7iJoaZ4oWZl0o0rluTtbtUPQRQKBgQD71JE3c4X4bta7SOUJ
C9dElZTy46o5gncgIa2qx3RHbbaCuQbVJmb3AHiyEjYToGLFgZDO3DhkA52MqUS7
hbO0xvXd2ZfJrqhownw6T1VZQbPAyuVFwFgEfDpG3cSpQgM99OFOLFjtvbimOFPe
4XpfYbT03gK9aQuljnwN5/BC+wKBgQDNT6+BRPZAUCOHQ173ibP6NWwEQcIwEPSL
mbsnCLo14Wws7/lHjivGOVysovRFTzb34TN1kqHBgU0e1Plturce2zI4Ie5pRwrH
nFNzPz+amy73BIdRTa1L9Fqd4Gm4j9XYCkTso/x8A6DrNt2UnTqsFgJmJtzYF+m/
6g4yK9GxxwKBgHjbPwX5ryXXK76d8S0yPZFwqBcZI6yN7FXDU/FN34QYJyr9WUYa
M/f+he4Px1wL8NsQn0pnbbix8356Db5hICl2ArEBqFLmO3RrQetJ/4/idD3mIboj
4Rnl9KHl9Ge2go/NYgN+TP9ruZ4sEjQ4yd3Uql+J3I7CRxChHPAfi7LZAoGBAJuY
/KW3ofjzwzlL8bkgf2ns+sPvIkBTWUJDa/cVQip7gQQ9imqUcNB1wKqFhSLR+hK6
dclxK23/lHb9aVuj2gxkixbHgGwBD0ZgT05UbNu7KEjFAdi4SdH6ioKEBRt+xs5I
WhwN29gQ1+/rUNrEnia1N3Q7l7udw+VSeRfE5dMZAoGAB9gQXNkGrbayf60T1s9b
vipXnSdshWJJJLP9mpHUAuonpLqlyL+UHa1J6XZDZOKrG/2Qqoccm0D/GwgbNV8j
8L797U4evrCof1XeM0hWVcEmscFGCuYBoHQ8/0C9QMdnhPGRPLcGt6oiMrXF8mc0
t66PYyekAwUaVM0F0FuoLgI=
-----END PRIVATE KEY-----"""


@pytest.fixture
def provider():
    cfg = GitHubAppConfig(app_id="999", private_key_pem=_TEST_PEM, webhook_secret="whsec")
    return GitHubAppProvider(cfg)


def test_app_jwt_is_signed(provider):
    tok = provider._app_jwt()
    assert tok.count(".") == 2


@pytest.mark.asyncio
async def test_installation_token_fetch_and_cache(provider, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://api.github.com/app/installations/12345/access_tokens",
        json={"token": "ghs_abc", "expires_at": "2099-01-01T00:00:00Z"},
        status_code=201,
    )
    t1 = await provider.get_installation_token(12345)
    assert t1 == "ghs_abc"
    t2 = await provider.get_installation_token(12345)
    assert t2 == "ghs_abc"


@pytest.mark.asyncio
async def test_list_repos(provider, httpx_mock):
    httpx_mock.add_response(
        url="https://api.github.com/app/installations/12345/access_tokens",
        method="POST", json={"token": "ghs_abc", "expires_at": "2099-01-01T00:00:00Z"}, status_code=201,
    )
    httpx_mock.add_response(
        url="https://api.github.com/installation/repositories?per_page=100&page=1",
        json={"total_count": 1, "repositories": [
            {"id": 42, "full_name": "macrozheng/mall-swarm", "default_branch": "master", "private": True}
        ]},
    )
    repos = await provider.list_repos(12345)
    assert len(repos) == 1
    assert repos[0].external_id == 42
    assert repos[0].full_name == "macrozheng/mall-swarm"
    assert repos[0].private is True


@pytest.mark.asyncio
async def test_list_branches(provider, httpx_mock):
    httpx_mock.add_response(
        url="https://api.github.com/app/installations/12345/access_tokens",
        method="POST", json={"token": "ghs_abc", "expires_at": "2099-01-01T00:00:00Z"}, status_code=201,
    )
    httpx_mock.add_response(
        url="https://api.github.com/repos/macrozheng/mall-swarm",
        json={"default_branch": "master"},
    )
    httpx_mock.add_response(
        url="https://api.github.com/repos/macrozheng/mall-swarm/branches?per_page=100&page=1",
        json=[{"name": "master"}, {"name": "dev"}],
    )
    bl = await provider.list_branches(12345, "macrozheng/mall-swarm")
    assert bl.default_branch == "master"
    assert set(bl.branches) == {"master", "dev"}
