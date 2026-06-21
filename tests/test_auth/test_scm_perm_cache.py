import pytest
from src.service.scm.base import ScmRole
from src.service.scm.scm_perm_cache import resolve_repo_role_cached, cache_clear, cache_invalidate


@pytest.fixture(autouse=True)
def _clear():
    cache_clear()
    yield
    cache_clear()


class _FakeProvider:
    def __init__(self, role, *, fail_if_called_twice=False):
        self._role = role
        self.calls = 0
    async def resolve_repo_role(self, *, token, repo, principal):
        self.calls += 1
        return self._role


@pytest.mark.asyncio
async def test_positive_cached_second_call_no_api():
    p = _FakeProvider(ScmRole.CAN_QUERY)
    kw = dict(user_id=1, connection_id="c1", repo_external_id=42, token="t", repo="o/r", principal="u")
    assert await resolve_repo_role_cached(p, **kw) == ScmRole.CAN_QUERY
    assert await resolve_repo_role_cached(p, **kw) == ScmRole.CAN_QUERY
    assert p.calls == 1   # 第二次命中缓存，不再调 provider


@pytest.mark.asyncio
async def test_deny_not_cached():
    p = _FakeProvider(ScmRole.NOT_VISIBLE)
    kw = dict(user_id=1, connection_id="c1", repo_external_id=42, token="t", repo="o/r", principal="u")
    assert await resolve_repo_role_cached(p, **kw) == ScmRole.NOT_VISIBLE
    assert await resolve_repo_role_cached(p, **kw) == ScmRole.NOT_VISIBLE
    assert p.calls == 2   # NOT_VISIBLE 不缓存，第二次重打


@pytest.mark.asyncio
async def test_can_bind_not_cached():
    # A3：can_bind 永不缓存——即使经 cached wrapper，CAN_BIND 结果也不入缓存
    p = _FakeProvider(ScmRole.CAN_BIND)
    kw = dict(user_id=1, connection_id="c1", repo_external_id=42, token="t", repo="o/r", principal="u")
    assert await resolve_repo_role_cached(p, **kw) == ScmRole.CAN_BIND
    assert await resolve_repo_role_cached(p, **kw) == ScmRole.CAN_BIND
    assert p.calls == 2   # CAN_BIND 不缓存，第二次重打


@pytest.mark.asyncio
async def test_invalidate():
    p = _FakeProvider(ScmRole.CAN_QUERY)
    kw = dict(user_id=1, connection_id="c1", repo_external_id=42, token="t", repo="o/r", principal="u")
    await resolve_repo_role_cached(p, **kw)
    cache_invalidate("c1")
    await resolve_repo_role_cached(p, **kw)
    assert p.calls == 2   # invalidate 后重打


@pytest.mark.asyncio
async def test_ttl_expiry(monkeypatch):
    import src.service.scm.scm_perm_cache as mod
    p = _FakeProvider(ScmRole.CAN_QUERY)
    kw = dict(user_id=1, connection_id="c1", repo_external_id=42, token="t", repo="o/r", principal="u")
    t = {"now": 1000.0}
    monkeypatch.setattr(mod, "_now", lambda: t["now"])
    await resolve_repo_role_cached(p, **kw)
    t["now"] += mod._TTL + 1     # 过期
    await resolve_repo_role_cached(p, **kw)
    assert p.calls == 2
