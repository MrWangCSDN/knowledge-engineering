import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select
from src.service.scm_router import create_scm_routes
from src.service.db_models_homepage import Base, ScmConnection


class _User:
    id = 1; username = "alice"; is_admin = True


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


def _app_install(maker, user=None):
    """install-url 专用 app：带 db（A 早拦要查 UserScmToken）。_get_db 镜像生产 commit 语义。"""
    from fastapi import FastAPI
    app = FastAPI()
    async def _get_db():
        async with maker() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise
    app.include_router(create_scm_routes(
        get_current_user=lambda: (user or _User()), get_db=_get_db,
        get_provider=lambda: None, app_slug="ke-test-app",
    ))
    return app


@pytest.mark.asyncio
async def test_install_url_with_link_returns_state_and_cookie(maker):
    from src.service.scm.scm_token_store import upsert_token
    async with maker() as s:                    # seed alice 的 github token 行
        await upsert_token(s, user_id=1, provider="github", access_token="AT",
                           refresh_token=None, expires_at=None, scopes=None, scm_login="alice")
        await s.commit()
    c = TestClient(_app_install(maker))
    r = c.get("/scm/github/install-url")
    assert r.status_code == 200
    body = r.json()
    assert "github.com/apps/ke-test-app/installations/new" in body["install_url"]
    assert body["state"]
    assert "state=" in body["install_url"]
    sc = r.headers.get("set-cookie", "")
    assert "ke_oauth_csrf=" in sc and "samesite=lax" in sc.lower() and "httponly" in sc.lower()


@pytest.mark.asyncio
async def test_install_url_without_link_forbidden(maker):
    c = TestClient(_app_install(maker))         # 不 seed token 行
    r = c.get("/scm/github/install-url")
    assert r.status_code == 403


class _InstallProvider:
    """callback 归属核验 fake：list_user_installations 返回可见安装；get_account_login 返回 login。"""
    def __init__(self, installs=(12345,), login="acme", installs_exc=None, login_exc=None):
        self._installs = list(installs); self._login = login
        self._installs_exc = installs_exc; self._login_exc = login_exc
    async def list_user_installations(self, *, user_token):
        if self._installs_exc:
            raise self._installs_exc
        return self._installs
    async def get_account_login(self, installation_id):
        if self._login_exc:
            raise self._login_exc
        return self._login


class _FakeOAuthCfg:        # build_refresh_fn 读 .github → 无该属性返回 None refresh_fn（token 未过期不会用到）
    pass


def _app_callback(maker, *, user=None, provider=None):
    """callback 专用 app：注入 get_login_provider + oauth_cfg。
    _get_db 必须镜像生产 db.get_db 的 commit/rollback 语义，否则 I4 replay-after-403 测试不确定。"""
    from fastapi import FastAPI
    app = FastAPI()
    prov = provider or _InstallProvider()
    async def _get_db():
        async with maker() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise
    app.include_router(create_scm_routes(
        get_current_user=lambda: (user or _User()), get_db=_get_db,
        get_provider=lambda: prov, app_slug="ke-test-app",
        oauth_cfg=_FakeOAuthCfg(), get_login_provider=lambda p, cfg: prov,
    ))
    return app


async def _seed_token(maker, *, user_id=1):
    from src.service.scm.scm_token_store import upsert_token
    async with maker() as s:
        await upsert_token(s, user_id=user_id, provider="github", access_token="AT",
                           refresh_token=None, expires_at=None, scopes=None, scm_login="alice")
        await s.commit()


async def _mint(maker, *, purpose="install", user_id=1):
    """铸一个真 state，返回 (state, csrf)。"""
    from src.service.scm.oauth_state_store import mint_state
    async with maker() as s:
        m = await mint_state(s, provider="github", purpose=purpose,
                             user_id=user_id, with_nonce=False, ttl_seconds=1800)
        await s.commit()
        return m.state, m.csrf


def _callback(c, *, installation_id, state=None, csrf=None):
    """对 callback 发请求。csrf 非 None 时在 client 实例上 set cookie（避 httpx0.28 per-request cookies= 弃用警告）。
    state=None 不带 state 参；csrf=None 不带 cookie。"""
    if csrf is not None:
        c.cookies.set("ke_oauth_csrf", csrf)
    params = {"installation_id": installation_id}
    if state is not None:
        params["state"] = state
    return c.get("/scm/github/callback", params=params)


class _FakeProvider:
    async def get_account_login(self, installation_id):
        return "macrozheng"


def _app_db(maker, provider):
    from fastapi import FastAPI
    app = FastAPI()
    async def _get_db():
        async with maker() as s:
            yield s
    app.include_router(create_scm_routes(
        get_current_user=lambda: _User(), get_db=_get_db,
        get_provider=lambda: provider, app_slug="ke-test-app",
    ))
    return app


@pytest.mark.asyncio
async def test_callback_creates_connection(maker):
    await _seed_token(maker)
    state, csrf = await _mint(maker)
    c = TestClient(_app_callback(maker))
    r = _callback(c, installation_id=12345, state=state, csrf=csrf)
    assert r.status_code == 200
    cid = r.json()["connection_id"]
    async with maker() as s:
        conn = (await s.execute(select(ScmConnection).where(ScmConnection.id == cid))).scalar_one()
        assert conn.github_installation_id == 12345
        assert conn.provider == "github"
        assert conn.auth_type == "github_app"
        assert conn.account_login == "acme"
        assert conn.status == "active"
        assert conn.created_by == "alice"


@pytest.mark.asyncio
async def test_callback_missing_state_rejected(maker):
    await _seed_token(maker)
    c = TestClient(_app_callback(maker))
    r = _callback(c, installation_id=12345)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_callback_missing_csrf_cookie_rejected(maker):
    await _seed_token(maker)
    state, _csrf = await _mint(maker)
    c = TestClient(_app_callback(maker))
    r = _callback(c, installation_id=12345, state=state)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_callback_wrong_purpose_rejected(maker):
    await _seed_token(maker)
    state, csrf = await _mint(maker, purpose="login")
    c = TestClient(_app_callback(maker))
    r = _callback(c, installation_id=12345, state=state, csrf=csrf)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_callback_wrong_user_rejected(maker):
    await _seed_token(maker, user_id=1)
    state, csrf = await _mint(maker, user_id=2)
    c = TestClient(_app_callback(maker))
    r = _callback(c, installation_id=12345, state=state, csrf=csrf)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_callback_no_token_forbidden(maker):
    state, csrf = await _mint(maker)
    c = TestClient(_app_callback(maker))
    r = _callback(c, installation_id=12345, state=state, csrf=csrf)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_callback_forged_installation_forbidden(maker):
    await _seed_token(maker)
    state, csrf = await _mint(maker)
    c = TestClient(_app_callback(maker, provider=_InstallProvider(installs=(12345,))))
    r = _callback(c, installation_id=99999, state=state, csrf=csrf)
    assert r.status_code == 403
    async with maker() as s:
        rows = (await s.execute(select(ScmConnection))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_callback_installations_upstream_error_502(maker):
    await _seed_token(maker)
    state, csrf = await _mint(maker)
    boom = httpx.HTTPStatusError("5xx", request=httpx.Request("GET", "https://x"),
                                 response=httpx.Response(503))
    c = TestClient(_app_callback(maker, provider=_InstallProvider(installs_exc=boom)))
    r = _callback(c, installation_id=12345, state=state, csrf=csrf)
    assert r.status_code == 502


@pytest.mark.asyncio
async def test_callback_account_login_5xx_502_no_write(maker):
    await _seed_token(maker)
    state, csrf = await _mint(maker)
    boom = httpx.HTTPStatusError("5xx", request=httpx.Request("GET", "https://x"),
                                 response=httpx.Response(502))
    c = TestClient(_app_callback(maker, provider=_InstallProvider(installs=(12345,), login_exc=boom)))
    r = _callback(c, installation_id=12345, state=state, csrf=csrf)
    assert r.status_code == 502
    async with maker() as s:
        rows = (await s.execute(select(ScmConnection))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_callback_empty_account_login_502_no_write(maker):
    await _seed_token(maker)
    state, csrf = await _mint(maker)
    c = TestClient(_app_callback(maker, provider=_InstallProvider(installs=(12345,), login="")))
    r = _callback(c, installation_id=12345, state=state, csrf=csrf)
    assert r.status_code == 502
    async with maker() as s:
        rows = (await s.execute(select(ScmConnection))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_callback_replay_after_403_then_success(maker):
    await _seed_token(maker)
    state, csrf = await _mint(maker)
    c = TestClient(_app_callback(maker, provider=_InstallProvider(installs=(12345,))))
    r1 = _callback(c, installation_id=99999, state=state, csrf=csrf)
    assert r1.status_code == 403
    r2 = _callback(c, installation_id=12345, state=state)
    assert r2.status_code == 200
    r3 = _callback(c, installation_id=12345, state=state)
    assert r3.status_code == 400


@pytest.mark.asyncio
async def test_list_and_delete_connections(maker):
    from fastapi.testclient import TestClient
    from src.service.db_models_homepage import ScmConnection
    async with maker() as s:
        s.add(ScmConnection(id="c1", provider="github", auth_type="github_app",
                            github_installation_id=1, account_login="o", status="active", created_by="alice"))
        await s.commit()
    c = TestClient(_app_db(maker, _FakeProvider()))
    r = c.get("/scm/connections")
    assert r.status_code == 200
    assert any(x["id"] == "c1" for x in r.json()["connections"])
    assert c.delete("/scm/connections/c1").status_code == 204
    assert all(x["id"] != "c1" for x in c.get("/scm/connections").json()["connections"])


@pytest.mark.asyncio
async def test_delete_connection_forbidden_for_non_owner(maker):
    """非拥有者、非管理员用户尝试删除他人连接，应得到 403，且连接不被删除。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.service.db_models_homepage import ScmConnection

    # bob 不是 admin，也不是 alice 创建的连接的拥有者
    class _Bob:
        username = "bob"; is_admin = False

    # 先插入一条 alice 创建的连接
    async with maker() as s:
        s.add(ScmConnection(id="c1", provider="github", auth_type="github_app",
                            github_installation_id=1, account_login="o", status="active", created_by="alice"))
        await s.commit()

    # 构造一个使用 bob 身份的独立 app
    app = FastAPI()
    async def _get_db():
        async with maker() as s:
            yield s
    app.include_router(create_scm_routes(
        get_current_user=lambda: _Bob(), get_db=_get_db,
        get_provider=lambda: _FakeProvider(), app_slug="ke-test-app",
    ))
    c = TestClient(app)

    # bob 尝试删除 c1，应返回 403
    assert c.delete("/scm/connections/c1").status_code == 403

    # 确认连接仍在 DB 中（bob 无权删除）
    async with maker() as s:
        row = (await s.execute(select(ScmConnection).where(ScmConnection.id == "c1"))).scalar_one_or_none()
        assert row is not None, "连接不应被无权限用户删除"


class _FakeProvider2(_FakeProvider):
    async def list_repos(self, installation_id):
        from src.service.scm.base import RepoInfo
        return [RepoInfo(external_id=42, full_name="o/r", default_branch="master", private=True)]
    async def list_branches(self, installation_id, full_name):
        from src.service.scm.base import BranchList
        return BranchList(default_branch="master", branches=["master", "dev"])


@pytest.mark.asyncio
async def test_list_repos_and_branches(maker):
    from fastapi.testclient import TestClient
    from src.service.db_models_homepage import ScmConnection
    async with maker() as s:
        s.add(ScmConnection(id="c1", provider="github", auth_type="github_app",
                            github_installation_id=7, account_login="o", status="active", created_by="alice"))
        await s.commit()
    c = TestClient(_app_db(maker, _FakeProvider2()))
    repos = c.get("/scm/connections/c1/repos").json()["repos"]
    assert repos[0]["external_id"] == 42 and repos[0]["full_name"] == "o/r"
    br = c.get("/scm/connections/c1/repos/o%2Fr/branches").json()
    assert br["default_branch"] == "master" and "dev" in br["branches"]


@pytest.mark.asyncio
async def test_list_repos_forbidden_for_non_owner(maker):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.service.db_models_homepage import ScmConnection

    # bob 不是 admin，也不是 alice 创建的连接的拥有者
    class _Bob:
        username = "bob"; is_admin = False

    # 先插入一条 alice 创建的连接
    async with maker() as s:
        s.add(ScmConnection(id="c1", provider="github", auth_type="github_app",
                            github_installation_id=7, account_login="o", status="active", created_by="alice"))
        await s.commit()

    # 构造一个使用 bob 身份的独立 app
    app = FastAPI()
    async def _get_db():
        async with maker() as s:
            yield s
    app.include_router(create_scm_routes(
        get_current_user=lambda: _Bob(), get_db=_get_db,
        get_provider=lambda: _FakeProvider2(), app_slug="ke-test-app",
    ))
    c = TestClient(app)
    # bob 尝试访问 alice 的连接，应返回 403
    assert c.get("/scm/connections/c1/repos").status_code == 403
