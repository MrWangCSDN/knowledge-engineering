# GitHub 连接 P4b-2 — App-install callback 严格归属核验 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `GET /scm/github/callback` 在写 `ScmConnection` **之前**严格核验该 installation 确属调用者（CSRF 绑定 state + 取 user token + `list_user_installations` 成员核验），并让 `install-url` 早拦未关联用户、签发真 state + csrf cookie。

**Architecture:** 仅改 `src/service/scm_router.py`（install-url + callback + 工厂签名）与 `src/service/api.py`（装配传 `oauth_cfg`/`get_login_provider`）。复用 P4a/P4b-0 既有件：`mint_state`/`consume_state`（CSRF+原子单用）、`get_valid_scm_token`（Fernet+刷新+fail-closed）、`build_refresh_fn`、`get_login_provider`、`list_user_installations`。**先核验后写**：所有 guard 在 `ScmConnection(...)` 构造之前；任一失败不 `db.add`/`commit`。**无 kill-switch**（修漏洞，且整特性未部署）。GitHub-only（GitLab 无 installation 概念）。

**Tech Stack:** FastAPI / SQLAlchemy async / httpx / pytest-asyncio / pytest（worktree 内 venv）。

**设计依据:** Obsidian `GitHub仓库连接-P4b-2-App安装归属核验-设计.md`（已过对抗评审，硬化 B1/B2+I1-4+M1-5）。

**测试运行约定（必带 env）:**
```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest <路径> -v
```
（`KE_COOKIE_SECURE=false` 必带，否则 TestClient 收不到 Secure cookie，set-cookie 断言假失败；`KE_TOKEN_ENC_KEY` 是合法 Fernet key，upsert_token 加密用。）

---

## 文件结构

| 文件 | 责任 | 动作 |
|---|---|---|
| `src/service/scm_router.py` | `_INSTALL_PURPOSE` 常量；install-url（A 早拦+真 state+csrf cookie）；callback（consume+token+membership+先核验后写）；工厂加 `oauth_cfg`/`get_login_provider` 参 | 改 |
| `src/service/api.py` | mount `create_scm_routes` 时传 `oauth_cfg`/`get_login_provider` | 改 |
| `tests/test_auth/test_scm_router.py` | `import httpx`；`_User` 加 `id`（callback 矩阵的 wrong-user 用 `_mint(user_id=2)` 制造不匹配，**无需**给局部 `_Bob` fake 加 id）；更新 `test_install_url`、`test_callback_creates_connection`；删除替换后无引用的 `_app()` 工厂；新增归属核验测试矩阵 | 改 |
| `tests/test_auth/test_scm_router_wiring.py` | 新建：AST 断言 `create_scm_routes(...)` mount 已接 `oauth_cfg`/`get_login_provider` | 建 |

**关键既有签名（照抄，勿臆造）：**
- `mint_state(session, *, provider, purpose, user_id, with_nonce, ttl_seconds=600) -> MintedState(state, csrf, nonce)`（flush-only）
- `consume_state(session, *, state, csrf) -> Optional[OAuthState]`（带回 `.purpose`/`.user_id`；缺/重放/过期/csrf 不符→None）
- `get_valid_scm_token(session, *, user_id, provider, refresh_fn) -> str`；失效抛 `ScmTokenInvalid`（已删行）
- `build_refresh_fn(provider, *, gitlab_provider=None, oauth_cfg=None) -> Optional[RefreshFn]`
- `get_login_provider(provider, oauth_cfg)`；未配抛 `OAuthProviderUnavailable`
- `list_user_installations(*, user_token) -> list[int]`（`raise_for_status`→`httpx.HTTPStatusError`）
- `upsert_token(session, *, user_id, provider, access_token, refresh_token, expires_at, scopes, scm_login) -> None`（测试 seed token 用）
- `UserScmToken`：字段 `user_id:int` / `provider:str` / `access_token`（Fernet 密文）/ `scm_login` / `expires_at`
- `ScmConnection` id 生成范式：`f"conn-{uuid.uuid4().hex[:16]}"`（`uuid` 已 import）

---

## Task 1: install-url 改造（A 早拦 + 真 state + csrf cookie）+ 工厂签名 + fake 带 id

**Files:**
- Modify: `src/service/scm_router.py`（imports、`_INSTALL_PURPOSE`、工厂签名、`install_url`）
- Test: `tests/test_auth/test_scm_router.py`（`_User` 加 `id`；重写 `test_install_url`；新增 403 测试）

### 背景（实现者必读）
当前 `install_url`（`scm_router.py:22-29`）只 `secrets.token_urlsafe(24)` 铸装饰性 state、不下 cookie、不查关联。本任务改为：先查 `UserScmToken(user.id, "github")` 存在性（无→403 引导先关联），再 `mint_state(purpose="install", ttl=1800)`，再 `response.set_cookie("ke_oauth_csrf", ..., samesite="lax", max_age=1800)`。工厂同时加 `oauth_cfg=None`/`get_login_provider=None` 两个**默认 None** 关键字参（Task 2 callback 用，这里一次性加好，避免二次改签名；defaulted 不破现有 mount）。

- [ ] **Step 1: 写失败测试 — install-url 已关联→200+state+csrf cookie，未关联→403**

在 `tests/test_auth/test_scm_router.py` 顶部 import 区补 `import httpx`（Task 2 的两个 502 用例在测试体内构造 `httpx.HTTPStatusError`，**测试文件自身需要**，与 scm_router.py 的 import 分开）。

把 `_User` 改为带 `id`：
```python
class _User:
    id = 1; username = "alice"; is_admin = True
```
> 注：本仓另两个局部 `_Bob` fake（`test_delete_connection_forbidden_for_non_owner`/`test_list_repos_forbidden_for_non_owner` 内）只需 username/is_admin，**不要**给它们加 id（callback 的 wrong-user 用例用 `_mint(user_id=2)` 制造 user_id 不匹配，不靠 `_Bob` 身份切换）。

新增一个 db-backed install-url 测试 app 工厂（放在 `maker` fixture 之后、`_FakeProvider` 之前）：
```python
def _app_install(maker, user=None):
    """install-url 专用 app：带 db（A 早拦要查 UserScmToken）。

    _get_db 镜像生产 get_db 的 commit 语义——install-url 的 mint_state 是 flush-only，
    依赖 get_db 末尾 commit 持久化 state（与 P4a oauth router 同范式，install-url 不显式 commit）。"""
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
```

把现有 `test_install_url`（`scm_router` 测试里那个无 db 的）**替换**为两个用例：
```python
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
    assert body["state"]                        # 真 state（非空）
    assert "state=" in body["install_url"]
    sc = r.headers.get("set-cookie", "")
    assert "ke_oauth_csrf=" in sc and "samesite=lax" in sc.lower() and "httponly" in sc.lower()


@pytest.mark.asyncio
async def test_install_url_without_link_forbidden(maker):
    c = TestClient(_app_install(maker))         # 不 seed token 行
    r = c.get("/scm/github/install-url")
    assert r.status_code == 403                  # A 早拦：未关联 GitHub
```

> **清理死代码**：替换 `test_install_url` 后，无 db 的 `_app(provider=None)` 工厂（原 `tests/test_auth/test_scm_router.py:15-23`）失去唯一引用，**删除它**。⚠️ 不要删 `_FakeProvider`/`_app_db`/`_FakeProvider2`——它们仍被 `test_list_and_delete_connections`/`test_list_repos_and_branches` 等用例引用。

- [ ] **Step 2: 运行测试，确认失败**

```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth/test_scm_router.py::test_install_url_with_link_returns_state_and_cookie tests/test_auth/test_scm_router.py::test_install_url_without_link_forbidden -v
```
预期：FAIL（install-url 当前不查 token、不下 cookie；A 早拦未实现）。

- [ ] **Step 3: 实现 — imports + 常量 + 工厂签名 + install-url**

在 `scm_router.py` 顶部 import 区补（`os`/`secrets`/`uuid`/`Optional`/`select`/`Response`/`ScmConnection`/`cache_invalidate` 已在）：
```python
import httpx
from fastapi import Cookie                                      # 加到现有 fastapi import 行
from src.service.db_models_homepage import UserScmToken         # 加到现有 ScmConnection import 行
from src.service.scm.oauth_state_store import mint_state, consume_state
from src.service.scm.scm_token_store import get_valid_scm_token, ScmTokenInvalid
from src.service.scm.scm_refresh import build_refresh_fn
from src.service.scm.oauth_factory import OAuthProviderUnavailable
```

在 import 之后、`create_scm_routes` 之前加常量：
```python
_INSTALL_PURPOSE = "install"   # install-url 与 callback 共用，防 purpose typo 静默 400
```

工厂签名加两个默认 None 参（callback 用，install-url 不用）：
```python
def create_scm_routes(*, get_current_user: Callable, get_db: Optional[Callable],
                      get_provider: Callable, app_slug: Optional[str] = None,
                      oauth_cfg=None, get_login_provider: Optional[Callable] = None) -> APIRouter:
```

把 `install_url` 整体替换为：
```python
    @router.get("/github/install-url")
    async def install_url(response: Response, user=Depends(get_current_user),
                          db=Depends(get_db)) -> dict:
        """返回 GitHub App 安装 URL + 真 state（CSRF 绑定）。A 早拦：未关联 GitHub → 403。"""
        # A 早拦（UX 轻量存在性查；真核验在 callback）：无 github 关联 → 403 引导先关联
        row = (await db.execute(select(UserScmToken).where(
            UserScmToken.user_id == user.id, UserScmToken.provider == "github"
        ))).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=403, detail="请先关联 GitHub 账号，再安装应用")
        minted = await mint_state(db, provider="github", purpose=_INSTALL_PURPOSE,
                                  user_id=user.id, with_nonce=False, ttl_seconds=1800)
        # mint_state 仅 flush；不显式 commit——由 get_db 末尾 commit 持久化 state（与 P4a oauth router 同范式）
        response.set_cookie(
            key="ke_oauth_csrf", value=minted.csrf, httponly=True,
            secure=os.getenv("KE_COOKIE_SECURE", "true").lower() == "true",
            samesite="lax", path="/", max_age=1800,
        )
        return {
            "install_url": f"https://github.com/apps/{slug}/installations/new?state={minted.state}",
            "state": minted.state,
        }
```
> 注：原 install-url 无 `db`/`response`，本任务加上；`secrets` import 若不再被其他处使用可保留（无害）。install-url **不显式 commit**——依赖 get_db 末尾 commit 持久化 state（生产 `db.get_db` 与测试 `_app_install._get_db` 都 commit-on-success；与 spec §4.1、P4a oauth router 同范式）。callback 则**显式 commit**（write 连接+消费 state 一并提交，见 Task 2）。

- [ ] **Step 4: 运行测试，确认通过**

```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth/test_scm_router.py::test_install_url_with_link_returns_state_and_cookie tests/test_auth/test_scm_router.py::test_install_url_without_link_forbidden -v
```
预期：PASS（2 passed）。

- [ ] **Step 5: Commit**

```bash
git add src/service/scm_router.py tests/test_auth/test_scm_router.py
git commit -m "feat(scm): install-url A 早拦 + 真 state + csrf cookie（P4b-2 T1）"
```

---

## Task 2: callback 改造（consume_state + 取 token + 成员核验 + 先核验后写）

**Files:**
- Modify: `src/service/scm_router.py`（`callback`）
- Test: `tests/test_auth/test_scm_router.py`（更新 `test_callback_creates_connection`；新增归属核验矩阵）

### 背景（实现者必读）
当前 `callback`（`scm_router.py:31-45`）盲信 query 的 `installation_id`，直接 `get_provider().get_account_login()` 后建连接。本任务把它改为 **先核验后写**：
1. `consume_state(db, state, csrf)` → `None`/`purpose≠install`/`user_id≠user.id` → 400
2. `prov = get_login_provider("github", oauth_cfg)`（`OAuthProviderUnavailable`→503）
3. `token = get_valid_scm_token(...)`（`ScmTokenInvalid`→403；`httpx.HTTPError`/`RuntimeError`→502）
4. `installs = prov.list_user_installations(user_token=token)`（`httpx.HTTPError`→502）；`installation_id ∉ installs`→403
5. `user.username is None`→403；`prov.get_account_login(installation_id)`（`httpx.HTTPError`→502）
6. 全过 → 才 `ScmConnection(...)` + `db.add` + `commit`

callback 改用 `prov`（`get_login_provider` 返回的 provider）做 `list_user_installations` + `get_account_login`，`get_login_provider` 是测试可注入的接缝。

### I4 单用语义（实现+测试都要懂）
`consume_state` 只 flush DELETE；`get_db` 在任何抛异常时回滚整事务。故 state 仅在"提交成功（写连接）"时才被持久消费；guard 拒绝（如伪造 installation_id→403）会回滚 DELETE，**同一 state+csrf 在 TTL 内可重试**，但每次重试都重跑全部 guard，无法产出未核验/未归属连接。replay-after-403 测试覆盖此。

- [ ] **Step 1: 写失败测试 — 更新现有 callback 测试 + 新增归属矩阵**

**前置**：确认 `tests/test_auth/test_scm_router.py` 顶部已 `import httpx`（Task 1 已加；若执行单独的 Task 2 则现在补上——下面两个 502 用例在测试体内构造 `httpx.HTTPStatusError`，缺它会 `NameError`）。

先扩充 `_FakeProvider`（让它能做归属核验），并加一个可注入 `get_login_provider`/`oauth_cfg` 的 callback 测试 app 工厂。在 `tests/test_auth/test_scm_router.py` 加：
```python
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

    _get_db 必须**镜像生产 db.get_db 的 commit/rollback 语义**（成功 commit、异常 rollback+raise）——
    否则 I4 replay-after-403 测试不确定：403 抛 HTTPException 必须触发 rollback 撤销 consume_state 的
    DELETE，state 才能在 TTL 内重试。"""
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
    """对 callback 发请求。csrf 非 None 时在 **client 实例**上 set cookie——
    避免 per-request `cookies=` 在 httpx 0.28 触发 DeprecationWarning（实例级 cookie jar 无此警告）。
    state=None 时不带 state 参（测 缺 state）；csrf=None 时不带 cookie（测 缺 csrf）。"""
    if csrf is not None:
        c.cookies.set("ke_oauth_csrf", csrf)
    params = {"installation_id": installation_id}
    if state is not None:
        params["state"] = state
    return c.get("/scm/github/callback", params=params)
```

更新现有 `test_callback_creates_connection`（它现在直接 `params={"installation_id":12345,"state":"s1"}`，会 400）：
```python
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
        assert conn.account_login == "acme"          # _InstallProvider 默认 login
        assert conn.status == "active"
        assert conn.created_by == "alice"
```

新增归属核验矩阵：
```python
@pytest.mark.asyncio
async def test_callback_missing_state_rejected(maker):
    await _seed_token(maker)
    c = TestClient(_app_callback(maker))
    r = _callback(c, installation_id=12345)                    # 无 state、无 csrf
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_callback_missing_csrf_cookie_rejected(maker):
    await _seed_token(maker)
    state, _csrf = await _mint(maker)
    c = TestClient(_app_callback(maker))
    r = _callback(c, installation_id=12345, state=state)       # 有 state、不带 cookie
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_callback_wrong_purpose_rejected(maker):
    await _seed_token(maker)
    state, csrf = await _mint(maker, purpose="login")          # 非 install purpose
    c = TestClient(_app_callback(maker))
    r = _callback(c, installation_id=12345, state=state, csrf=csrf)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_callback_wrong_user_rejected(maker):
    await _seed_token(maker, user_id=1)
    state, csrf = await _mint(maker, user_id=2)                # state 属 user 2
    c = TestClient(_app_callback(maker))                       # 当前身份是 user 1（alice）
    r = _callback(c, installation_id=12345, state=state, csrf=csrf)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_callback_no_token_forbidden(maker):
    state, csrf = await _mint(maker)                           # 不 seed token 行
    c = TestClient(_app_callback(maker))
    r = _callback(c, installation_id=12345, state=state, csrf=csrf)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_callback_forged_installation_forbidden(maker):
    await _seed_token(maker)
    state, csrf = await _mint(maker)
    c = TestClient(_app_callback(maker, provider=_InstallProvider(installs=(12345,))))
    r = _callback(c, installation_id=99999, state=state, csrf=csrf)   # 不在可见列表
    assert r.status_code == 403
    async with maker() as s:                                   # 确认未落库
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
    """I3：membership 通过后 get_account_login 5xx → 502，且不落库。"""
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
    """M-e：membership 通过但 get_account_login 返回空串（GitHub 响应异常）→ 502 fail-closed，不写脏行。"""
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
    """I4 单用语义：伪造 installation→403（state 回滚未消费）→ 同 state+csrf 重试合法 installation→200；
    再用同一 state 第三次→400（已 commit 消费）。"""
    await _seed_token(maker)
    state, csrf = await _mint(maker)
    c = TestClient(_app_callback(maker, provider=_InstallProvider(installs=(12345,))))
    # 1) 伪造 installation → 403，state 因事务回滚未持久消费（csrf 设到 client cookie jar，后续复用）
    r1 = _callback(c, installation_id=99999, state=state, csrf=csrf)
    assert r1.status_code == 403
    # 2) 同一 state+csrf 重试合法 installation → 200 落库
    r2 = _callback(c, installation_id=12345, state=state)      # cookie 已在 jar，无需再传 csrf
    assert r2.status_code == 200
    # 3) 同一 state 第三次 → 400（已 commit 消费，单用生效）
    r3 = _callback(c, installation_id=12345, state=state)
    assert r3.status_code == 400
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth/test_scm_router.py -k callback -v
```
预期：FAIL（callback 当前无 consume/token/membership guard；`test_callback_creates_connection` 因带 state/csrf 但 callback 不读 cookie 也会偏离）。

- [ ] **Step 3: 实现 — 重写 callback**

把 `callback` 整体替换为（**所有 guard 在 `ScmConnection(...)` 之前**）：
```python
    @router.get("/github/callback")
    async def callback(installation_id: int, state: str = "", user=Depends(get_current_user),
                       db=Depends(get_db),
                       ke_oauth_csrf: Optional[str] = Cookie(default=None)) -> dict:
        """GitHub App 安装回调：先核验该 installation 确属调用者，再建 scm_connection。"""
        # 1) 消费 state（CSRF 绑定、原子单用）；purpose/user_id 必须匹配
        st = await consume_state(db, state=state, csrf=ke_oauth_csrf)
        if st is None or st.purpose != _INSTALL_PURPOSE or st.user_id != user.id:
            raise HTTPException(status_code=400, detail="state 校验失败")
        # 2) 取调用者 user-to-server token（真核验前提）
        try:
            prov = get_login_provider("github", oauth_cfg)
        except OAuthProviderUnavailable:
            raise HTTPException(status_code=503, detail="github 未配置")
        refresh_fn = build_refresh_fn("github", oauth_cfg=oauth_cfg)
        try:
            token = await get_valid_scm_token(db, user_id=user.id, provider="github",
                                              refresh_fn=refresh_fn)
        except ScmTokenInvalid:
            raise HTTPException(status_code=403, detail="请先关联 GitHub 账号")
        except (httpx.HTTPError, RuntimeError):
            raise HTTPException(status_code=502, detail="SCM 授权刷新失败，请重试")
        # 3) 核 installation 归属（用户可见即可绑）
        try:
            installs = await prov.list_user_installations(user_token=token)
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail="校验安装归属失败，请重试")
        if installation_id not in installs:
            raise HTTPException(status_code=403, detail="该安装不属于当前用户")
        # 4) 通过 → 才写连接
        if getattr(user, "username", None) is None:
            raise HTTPException(status_code=403, detail="无效用户")
        try:
            login = await prov.get_account_login(installation_id)
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail="获取安装账号信息失败，请重试")
        if not login:                           # M-e：空 login（GitHub 响应缺 account.login）视为上游异常，fail-closed 不写脏行
            raise HTTPException(status_code=502, detail="获取安装账号信息失败，请重试")
        conn = ScmConnection(
            id=f"conn-{uuid.uuid4().hex[:16]}", provider="github", auth_type="github_app",
            github_installation_id=installation_id, account_login=login,
            status="active", created_by=user.username,
        )
        db.add(conn)
        await db.commit()
        return {"connection_id": conn.id, "account_login": login}
```

- [ ] **Step 4: 运行测试，确认通过**

```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth/test_scm_router.py -v
```
预期：PASS（含原有 list/delete/repos/branches 测试 + 新增 callback 矩阵，全绿）。

- [ ] **Step 5: Commit**

```bash
git add src/service/scm_router.py tests/test_auth/test_scm_router.py
git commit -m "feat(scm): callback 先核验后写——consume_state+token+installation 归属（P4b-2 T2）"
```

---

## Task 3: api.py 装配 + 全量回归 + import 冒烟

**Files:**
- Modify: `src/service/api.py`（`create_scm_routes` mount 传 `oauth_cfg`/`get_login_provider`）
- Test: 全量 `tests/test_auth` + `python -c "import src.service.api"` 冒烟

### 背景
`api.py:101-103` 当前 mount（**实测仅 3 参，未传 app_slug**——工厂内部 `slug = app_slug or os.getenv("KE_GH_APP_SLUG","")` 兜底）：
```python
app.include_router(create_scm_routes(
    get_current_user=get_current_user, get_db=get_db, get_provider=get_github_provider,
))
```
`get_login_provider` 与 `load_oauth_config` 已在 `api.py` 顶部 import（行 38/39）；现 `_authorize_scm` 处（行 108）已调一次 `load_oauth_config()`，P4a oauth router（行 115）另调一次。本任务**只给 create_scm_routes mount 追加** `oauth_cfg`/`get_login_provider` 两参；把 `_authorize_scm` 那处的 `load_oauth_config()` 提成局部 `_oauth_cfg` 给 mount + `_authorize_scm` 复用即可（**不动行 115 的 oauth router 装配——那是 P4a 范围，本片不越界**）。

> ⚠️ **评审教训（不要用源码子串断言）**：`"get_login_provider=get_login_provider" in src` / `"oauth_cfg=" in src` 在**未改的 api.py 里已恒为真**（行 108/115 已含同名 kwarg）——会让 RED 阶段假绿、且就算忘了给 create_scm_routes 接线测试也照绿。必须用 **AST 锚定到 `create_scm_routes(...)` 这一处调用**。

- [ ] **Step 1: 写失败测试 — AST 校验 create_scm_routes mount 已接两参**

新增 `tests/test_auth/test_scm_router_wiring.py`：
```python
"""校验 api.py 给 create_scm_routes(...) 这一处 mount 装配了 oauth_cfg/get_login_provider（callback 闭包参）。
用 AST 锚定到 create_scm_routes 调用，而非全文件子串——后者在本仓恒为真（行 108/115 已含同名 kwarg）。"""
import ast
import inspect


def test_create_scm_routes_mount_wires_callback_params():
    import src.service.api as api                       # import 冒烟：装配期不抛即通过这一半
    tree = ast.parse(inspect.getsource(api))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "create_scm_routes"]
    assert len(calls) == 1, f"期望恰好一处 create_scm_routes 调用，实得 {len(calls)}"
    kw = {k.arg for k in calls[0].keywords}
    assert "oauth_cfg" in kw, "create_scm_routes mount 缺 oauth_cfg 接线"
    assert "get_login_provider" in kw, "create_scm_routes mount 缺 get_login_provider 接线"
```
> 说明：`ast.walk` 找到 `create_scm_routes(...)` 这个 Call 节点，断言其关键字参数集合含 `oauth_cfg` 与 `get_login_provider`。未接线时这两个 kwarg 不在该调用上 → 断言失败（真 RED）；接线后 → PASS。`import src.service.api` 本身即冒烟（装配期 `load_oauth_config()` 未配 provider 返回 None 不抛）。

- [ ] **Step 2: 运行测试，确认失败**

```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth/test_scm_router_wiring.py -v
```
预期：FAIL（`create_scm_routes` mount 的 keywords 尚不含 `oauth_cfg`/`get_login_provider`——AST 断言对未改 api.py 真红）。

- [ ] **Step 3: 实现 — 给 mount 追加两参（复用 _oauth_cfg）**

把现有 3 参 mount **追加**两参，并把 `_authorize_scm` 那处的 `load_oauth_config()` 提成 `_oauth_cfg` 复用：
```python
_oauth_cfg = load_oauth_config()
app.include_router(create_scm_routes(
    get_current_user=get_current_user, get_db=get_db, get_provider=get_github_provider,
    oauth_cfg=_oauth_cfg, get_login_provider=get_login_provider,
))
...
_authorize_scm = create_authorize_scm(oauth_cfg=_oauth_cfg, get_login_provider=get_login_provider)
```
> 注：**不要**新增 `app_slug=`（现状 mount 本就没传，工厂有 env 兜底，加上只是无意义 diff）。**不要**改行 115 的 `create_scm_oauth_routes(..., oauth_config=load_oauth_config())`——那是 P4a 范围，本片不碰（它独立再调一次 load_oauth_config() 无副作用、幂等，可接受）。

- [ ] **Step 4: 运行测试，确认通过**

```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth/test_scm_router_wiring.py -v
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -c "import src.service.api; print('import ok')"
```
预期：wiring 测试 PASS；import 打印 `import ok`。

- [ ] **Step 5: 全量回归**

```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth -q
```
预期：全绿（P4b-1 基线 1054 passed + 本片新增用例；无回归）。

- [ ] **Step 6: Commit**

```bash
git add src/service/api.py tests/test_auth/test_scm_router_wiring.py
git commit -m "feat(scm): api.py 装配 callback 闭包参 oauth_cfg/get_login_provider（P4b-2 T3）"
```

---

## 验收标准（P4b-2 Done）

1. **install-url**：已关联→200 + 非空 state + `ke_oauth_csrf` cookie（samesite=lax/httponly）；未关联→403（A 早拦）。
2. **callback**：缺 state / 缺 csrf cookie / purpose≠install / user_id 不符→400；无 token→403；`installation_id ∉` 用户可见安装→403；可见→建连接（created_by/github_installation_id/account_login 正确）；上游 httpx 5xx（list_user_installations 或 get_account_login）→502；空 account_login→502（M-e）。
3. **refuse-before-write**：任一 guard 失败不留 `ScmConnection` 行（forged-installation、account_login-5xx、空-login 用例均显式断言空表）。
4. **单用语义**：replay-after-403→重试成功→第三次 400（I4 用例）。
5. **装配**：api.py `create_scm_routes(...)` mount 传 `oauth_cfg`/`get_login_provider`（AST wiring 测试守护，对未接线真红）；`import src.service.api` 冒烟通过。
6. **回归**：全量 `tests/test_auth` 绿。

## 自审记录（writing-plans self-review）

- **Spec 覆盖**：A 早拦→T1；consume/token/membership/先核验后写→T2；装配→T3；§6 全部用例（含 I3 account_login-5xx、I4 replay）映射到 T1/T2 用例；✅ 全覆盖。
- **类型一致**：`mint_state`/`consume_state`/`get_valid_scm_token`/`build_refresh_fn`/`list_user_installations`/`upsert_token`/`ScmConnection` 字段名均按 worktree 实际签名核对（`access_token` 非 `token_encrypted`；`st.purpose`/`st.user_id`）。✅
- **占位符**：无 TBD/TODO（仅引用既有代码现状的描述）。✅
- **风险点**：(a) `install-url` **不显式 commit**，依赖 get_db 末尾 commit（spec §4.1/P4a 范式；测试 fake `_get_db` 镜像生产 commit-on-success）；(b) callback 用注入的 `get_login_provider` 作 provider 源（测试接缝）；(c) fake `oauth_cfg` 无 `.github` → `build_refresh_fn` 返 None，token 未过期不触发 refresh，happy path 安全。

## 对抗评审修订记录（4 维度 × adversarial verify，reviewer 跑真代码核实）

- **BLOCKER F1**：测试文件缺 `import httpx`（两个 502 用例体内构造 httpx 异常）→ 已在 T1 Step 1 + T2 Step 1 双处加 `import httpx`。
- **IMPORTANT F2-F5（同一根因，4 维度各报一次）**：Task 3 原 wiring 测试用 `"...=" in src` 源码子串断言，对未改 api.py 恒为真（行 108/115 已含同名 kwarg）→ RED 假绿且不守护 mount 接线。已改为 **AST 锚定 `create_scm_routes(...)` 调用、断言其 keywords 含 `oauth_cfg`/`get_login_provider`**（对未接线真红）。
- **MINOR**：M-a/M-d 现状 mount 实为 3 参（无 app_slug）→ 背景与示例改正、只追加两参；M-b 替换 `test_install_url` 后 `_app()` 成死代码→ 加删除指引（保留 `_FakeProvider`/`_app_db`/`_FakeProvider2`）；M-c per-request `cookies=` 触发 httpx0.28 弃用警告→ 改 `_callback` 助手用实例级 cookie jar；M-e 空 `account_login` 会落脏行→ callback 加 `if not login: 502` + 新增用例；M-f「只调一次 load_oauth_config」过度声明（行 115 仍独立调）→ 改为只复用 mount+authorize_scm、明确不碰 P4a 行 115；M-g wrong-user 用 `_mint(user_id=2)` 替代 `_Bob` 身份切换→ 文件结构表与 Task 1 注同步。
- **REJECTED（2，verify 判 NOT_A_BUG）**：① "worktree 已含 P4b-2 探针/router 被改" = 评审期捕获的瞬时脏快照，实测 `git diff HEAD scm_router.py` 为空、clean P3 基线（已二次确认）；残留 inert 孤儿 `.pyc` 已删。② "test_conn_delete_invalidate 复用 create_scm_routes" 靠默认参存活、本就绿，仅文档可追溯性诉求，非缺陷。
