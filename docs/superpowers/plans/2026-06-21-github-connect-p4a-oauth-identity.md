# GitHub 连接 P4a：OAuth/OIDC 登录 + 身份映射 + per-user token 存储 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL：superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans，逐任务执行。Steps 用 `- [ ]`。

**Goal:** 让已有 KE 账号用 GitHub OAuth / GitLab OIDC 登录，把 KE `users` 关联到稳定 SCM 数字 id，并安全存 per-user token（含刷新），为 P4b/P4c 打地基。

**Architecture:** 手动 httpx 跑授权码流的 token 交换（OAuth2 token POST 极简、易 pytest-httpx mock）；`oauth_state` 表是 state/nonce 唯一真相源（CSRF cookie 绑定 + 原子一次性消费）；**link-only**（仅关联已有账号，未关联登录→403）；新 `user_scm_token` 表（Fernet 加密 + rotation-aware 刷新）；登录复用密码登录的双 token（access 体 + refresh cookie）。注入式路由工厂便于测试。
> **库选型（细化 spec §3.5）**：spec 原写"Authlib"，落地精化为 **joserfc 验 OIDC id_token（JWKS/alg 锚定）+ 手动 httpx 做 token POST**——authlib 的价值主要在 OIDC，而 joserfc 已覆盖且 httpx token 交换更易 mock，故**不引入 authlib**。

**Tech Stack:** FastAPI / SQLAlchemy async / Alembic / httpx(token 交换) / joserfc(JWKS 验签 + id_token) / cryptography(Fernet) / pytest + pytest-httpx + TestClient。复用 `auth_security`(JWT/cookie)、`token_crypto`(Fernet)、`get_current_user`/`get_db`、`ScmProvider`/`ScmIdentity`、`GitHubAppProvider`。

**设计依据:** [[GitHub仓库连接-P4a-OAuth登录与身份映射-设计]]（spec，过对抗评审）；母设计 [[GitHub仓库连接-设计]]、[[身份与授权模型-设计]]。

**前置:** P1+P2+P3 已在本分支。worktree `/Users/java/ke-github-connect`，分支 `feat/github-repo-connect`。测试统一前缀环境变量：
```
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=$(./venv/bin/python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())") ./venv/bin/python -m pytest <path>
```
> 注 1：`KE_TOKEN_ENC_KEY` 必须是合法 Fernet key（44 字节 base64）。为稳定可在测试会话导出固定值：
> `export KE_TOKEN_ENC_KEY=$(./venv/bin/python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")`，后续命令复用。
> 注 2：**`KE_COOKIE_SECURE=false` 必须设**（B2）——否则 callback 下发的 Secure cookie 在 `http://testserver` 上被 httpx cookie jar 丢弃，`ke_oauth_csrf` 取不到 → `consume_state` 返 None → 回调 400，所有 callback 测试假失败。建议测试会话 `export KE_COOKIE_SECURE=false`。

**关键既有 API（实现时直接用，勿臆造）:**
- `src/service/token_crypto.py`：`encrypt_token(plain:str)->str`、`decrypt_token(cipher:str)->str`、`reset_fernet_cache()`。`KE_TOKEN_ENC_KEY` 缺失→`encrypt_token`/`decrypt_token` 抛 `RuntimeError`。
- `src/service/auth_security.py`（实现里 `import src.service.auth_security as sec`）：`create_access_token(*,user_id:int,username:str)->str`、`create_refresh_token(*,user_id:int,remember_me:bool)->str`、`cookie_settings(remember_me:bool)->dict`（含 `key="refresh_token"`、`path`、`samesite`…）、`access_ttl_seconds()->int`。
- `auth_router.login` 范式：`resp.set_cookie(value=refresh, **sec.cookie_settings(False))`；登录校验 `locked_until>now`→423、`not is_active`→视同失败。
- `src/service/scm/base.py`：`ScmProvider`(Protocol)、`ScmIdentity(provider:str, scm_user_id:str, login:Optional[str])`（frozen dataclass）。
- `src/service/scm/config.py`：`@dataclass(frozen=True)` + `load_*_config()` 范式（缺 env 抛 RuntimeError）。
- `User` 在 `src/service/auth_models.py`（`__tablename__="users"`，与 `db_models_homepage` 共享 `from src.service.db import Base`）。
- 当前 alembic 单一 head：`project_scm_binding_v1`。

**范围边界:** 不做 authorize() SCM∩KE 门禁、SCM-role 解析、App-install 归属核验、缓存层、/visible-repos（全 P4b/P4c）；不做 created_by→users.id 迁移、多身份子表、自助建号、GitLab 多租户 issuer。

---

## 文件结构

| 文件 | 责任 |
|---|---|
| `pyproject.toml`（改） | 声明 joserfc（OIDC/JWKS 验签；不引 authlib） |
| `src/service/scm/config.py`（改） | 加 `OAuthConfig`/`load_oauth_config`（GitHub OAuth + GitLab OIDC，per-provider 可空） |
| `src/service/auth_models.py`（改） | `User` 加 `github_user_id`/`gitlab_sub` 列 |
| `src/service/db_models_homepage.py`（改） | 加 `UserScmToken` + `OAuthState` 模型 |
| `alembic/versions/oauth_identity_v1.py` | users 加列 + 两新表（单一 head） |
| `src/service/scm/oauth_state_store.py` | mint/原子消费 state + csrf hash + GC（纯函数 + DB） |
| `src/service/scm/scm_token_store.py` | upsert/取/删 user_scm_token（Fernet）+ `get_valid_scm_token` 刷新 |
| `src/service/scm/github_app.py`（改） | `get_login_identity` + GitHub OAuth flow（authorize URL / exchange） |
| `src/service/scm/gitlab_oidc.py` | `GitLabOidcProvider`（discovery/authorize/exchange/id_token 校验/get_login_identity） |
| `src/service/scm/oauth_factory.py` | `get_login_provider(provider)` 选 provider（fail-closed） |
| `src/service/scm_oauth_router.py` | login/callback/link-scm start/scm-links 路由 |
| `src/service/api.py`（改） | 挂 oauth router |
| `tests/test_auth/test_oauth_config.py` / `test_oauth_models.py` / `test_oauth_state_store.py` / `test_scm_token_store.py` / `test_github_login_identity.py` / `test_gitlab_oidc.py` / `test_oauth_factory.py` / `test_scm_oauth_router.py` | 测试 |

---

## Task 1：声明依赖（joserfc）

**Files:** Modify `pyproject.toml`

> 只引 joserfc（验 OIDC id_token + JWKS）。**不引 authlib**——token 交换走手动 httpx（更易测）。joserfc 已装于 venv。

- [ ] **Step 1: 加依赖**
在 `pyproject.toml` 的 `dependencies` 数组里（`python-jose` 附近）加一行：
```toml
    "joserfc>=1.0",
```

- [ ] **Step 2: 冒烟 import（已装于 venv，应直接通过）**
Run:
```
cd /Users/java/ke-github-connect && ./venv/bin/python -c "import joserfc; from joserfc import jwt as jjwt; from joserfc.jwk import KeySet, RSAKey; print('ok')"
```
Expected: `ok`

- [ ] **Step 3: 提交**
```bash
git add pyproject.toml && git commit -m "build(scm): 声明 joserfc（P4a OIDC id_token 验签）"
```

---

## Task 2：OAuthConfig 加载器

**Files:** Modify `src/service/scm/config.py`；Test `tests/test_auth/test_oauth_config.py`

> per-provider 可空：某 provider env 未配 → 该 provider 配置为 `None`（路由层 fail-closed 503）。`redirect_base` 来自 `KE_OAUTH_REDIRECT_BASE`。

- [ ] **Step 1: 失败测试**
```python
# tests/test_auth/test_oauth_config.py
import pytest
from src.service.scm.config import load_oauth_config


def test_github_configured(monkeypatch):
    monkeypatch.setenv("KE_OAUTH_REDIRECT_BASE", "https://ke.example.com")
    monkeypatch.setenv("KE_GH_OAUTH_CLIENT_ID", "gh-cid")
    monkeypatch.setenv("KE_GH_OAUTH_CLIENT_SECRET", "gh-sec")
    monkeypatch.delenv("KE_GITLAB_OIDC_ISSUER", raising=False)
    cfg = load_oauth_config()
    assert cfg.redirect_base == "https://ke.example.com"
    assert cfg.github is not None and cfg.github.client_id == "gh-cid"
    assert cfg.gitlab is None            # 未配 → None（fail-closed）


def test_gitlab_configured(monkeypatch):
    monkeypatch.setenv("KE_OAUTH_REDIRECT_BASE", "https://ke.example.com")
    monkeypatch.delenv("KE_GH_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.setenv("KE_GITLAB_OIDC_ISSUER", "https://gitlab.example.com")
    monkeypatch.setenv("KE_GITLAB_OIDC_CLIENT_ID", "gl-cid")
    monkeypatch.setenv("KE_GITLAB_OIDC_CLIENT_SECRET", "gl-sec")
    cfg = load_oauth_config()
    assert cfg.github is None
    assert cfg.gitlab is not None and cfg.gitlab.issuer == "https://gitlab.example.com"


def test_provider_for_helper(monkeypatch):
    monkeypatch.setenv("KE_OAUTH_REDIRECT_BASE", "https://ke.example.com")
    monkeypatch.setenv("KE_GH_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("KE_GH_OAUTH_CLIENT_SECRET", "y")
    monkeypatch.delenv("KE_GITLAB_OIDC_ISSUER", raising=False)
    cfg = load_oauth_config()
    assert cfg.provider("github") is cfg.github
    assert cfg.provider("gitlab") is None
```

- [ ] **Step 2: 跑测试确认失败**
Run: `... ./venv/bin/python -m pytest tests/test_auth/test_oauth_config.py -v` → ImportError `load_oauth_config`。

- [ ] **Step 3: 实现（追加到 `src/service/scm/config.py` 末尾）**
```python
from typing import Optional   # 顶部已有 import os；若无 Optional 则在顶部补


@dataclass(frozen=True)
class GitHubOAuthConfig:
    client_id: str
    client_secret: str


@dataclass(frozen=True)
class GitLabOidcConfig:
    issuer: str          # 形如 https://gitlab.example.com（discovery: {issuer}/.well-known/openid-configuration）
    client_id: str
    client_secret: str


@dataclass(frozen=True)
class OAuthConfig:
    redirect_base: str
    github: Optional[GitHubOAuthConfig]
    gitlab: Optional[GitLabOidcConfig]

    def provider(self, name: str) -> "Optional[object]":
        """按名取 provider 配置；未配置返回 None（路由层据此 fail-closed 503）。"""
        return {"github": self.github, "gitlab": self.gitlab}.get(name)


def load_oauth_config() -> OAuthConfig:
    """从 env 装配 OAuth 配置。某 provider 凭证缺失 → 该 provider 为 None（不抛，路由层 503）。"""
    redirect_base = os.getenv("KE_OAUTH_REDIRECT_BASE", "").strip()
    gh_id = os.getenv("KE_GH_OAUTH_CLIENT_ID", "").strip()
    gh_sec = os.getenv("KE_GH_OAUTH_CLIENT_SECRET", "").strip()
    github = GitHubOAuthConfig(gh_id, gh_sec) if (gh_id and gh_sec) else None
    gl_iss = os.getenv("KE_GITLAB_OIDC_ISSUER", "").strip()
    gl_id = os.getenv("KE_GITLAB_OIDC_CLIENT_ID", "").strip()
    gl_sec = os.getenv("KE_GITLAB_OIDC_CLIENT_SECRET", "").strip()
    gitlab = GitLabOidcConfig(gl_iss, gl_id, gl_sec) if (gl_iss and gl_id and gl_sec) else None
    return OAuthConfig(redirect_base=redirect_base, github=github, gitlab=gitlab)
```

- [ ] **Step 4: 跑测试确认通过**（3 passed）。

- [ ] **Step 5: 提交**
```bash
git add src/service/scm/config.py tests/test_auth/test_oauth_config.py && git commit -m "feat(scm): OAuthConfig 加载器（GitHub OAuth + GitLab OIDC，per-provider 可空）"
```

---

## Task 3：DB 模型 + 迁移（users 身份列 + user_scm_token + oauth_state）

**Files:** Modify `src/service/auth_models.py`、`src/service/db_models_homepage.py`；Create `alembic/versions/oauth_identity_v1.py`；Test `tests/test_auth/test_oauth_models.py`

- [ ] **Step 1: 失败测试**
```python
# tests/test_auth/test_oauth_models.py
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
```

- [ ] **Step 2: 跑测试确认失败**（ImportError `UserScmToken` / User 无 github_user_id）。

- [ ] **Step 3a: User 加列**（`src/service/auth_models.py`，`User` 类内；顶部 import 已有 `Integer, String`，需补 `BigInteger`）
```python
from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, func   # 改这一行，加 BigInteger
```
```python
    # ── SCM 身份链（P4a，设计 §5）；身份键=SCM 数字 id，UNIQUE ──
    github_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, unique=True)
    gitlab_sub: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, unique=True)
```

- [ ] **Step 3b: 两新表**（`src/service/db_models_homepage.py`，放在 `ScmConnection` 之后；该文件已 import `BigInteger, DateTime, ForeignKey, Integer, String, Text, text` 等，`datetime` 已 import）
```python
class UserScmToken(Base):
    """per-user SCM OAuth/OIDC token（Fernet 加密落库）。设计 §5。"""
    __tablename__ = "user_scm_token"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)        # github | gitlab
    access_token: Mapped[str] = mapped_column(Text, nullable=False)          # Fernet 密文
    refresh_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # Fernet 密文
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    scopes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)       # Text 防长 scope 截断
    scm_login: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)  # 展示，每次 upsert 刷新
    linked_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)    # 首次关联一次，复写不 bump
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=text("(CURRENT_TIMESTAMP)"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=text("(CURRENT_TIMESTAMP)"), nullable=False
    )
    __table_args__ = (UniqueConstraint("user_id", "provider", name="uq_user_scm_token_user_provider"),)


class OAuthState(Base):
    """OAuth/OIDC 授权码流的 state/nonce（服务端一次性消费 + CSRF cookie 绑定）。设计 §6。"""
    __tablename__ = "oauth_state"

    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256(state)
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)     # sha256(ke_oauth_csrf cookie)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)       # login | link
    user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True) # link 发起者
    nonce: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=text("(CURRENT_TIMESTAMP)"), nullable=False
    )
```
确认 `db_models_homepage.py` 顶部 `from sqlalchemy import (...)` 块含两新表用到的全部名字：`Integer, String, Text, DateTime, ForeignKey, UniqueConstraint, text`（缺哪个补哪个；多数已在，`UniqueConstraint` 往往需补）。`BigInteger` 只在 `auth_models.py`（Step 3a）需要，已单独处理。

- [ ] **Step 4: 跑测试确认通过**（3 passed）。

- [ ] **Step 5: 迁移**（先 `./venv/bin/alembic heads` 确认 head = `project_scm_binding_v1`）
```python
# alembic/versions/oauth_identity_v1.py
"""oauth identity v1: users 身份列 + user_scm_token + oauth_state

Revision ID: oauth_identity_v1
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "oauth_identity_v1"
down_revision: Union[str, Sequence[str], None] = "project_scm_binding_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as b:
        b.add_column(sa.Column("github_user_id", sa.BigInteger(), nullable=True))
        b.add_column(sa.Column("gitlab_sub", sa.String(length=255), nullable=True))
        b.create_unique_constraint("uq_users_github_user_id", ["github_user_id"])
        b.create_unique_constraint("uq_users_gitlab_sub", ["gitlab_sub"])

    op.create_table(
        "user_scm_token",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("scopes", sa.Text(), nullable=True),
        sa.Column("scm_login", sa.String(length=255), nullable=True),
        sa.Column("linked_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.UniqueConstraint("user_id", "provider", name="uq_user_scm_token_user_provider"),
    )
    op.create_table(
        "oauth_state",
        sa.Column("state_hash", sa.String(length=64), primary_key=True),
        sa.Column("csrf_hash", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("nonce", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("oauth_state")
    op.drop_table("user_scm_token")
    with op.batch_alter_table("users") as b:
        b.drop_constraint("uq_users_gitlab_sub", type_="unique")
        b.drop_constraint("uq_users_github_user_id", type_="unique")
        b.drop_column("gitlab_sub")
        b.drop_column("github_user_id")
```

- [ ] **Step 6: 确认单一 head**
Run: `... ./venv/bin/alembic heads` → 只有 `oauth_identity_v1 (head)`。

- [ ] **Step 7: 提交**
```bash
git add src/service/auth_models.py src/service/db_models_homepage.py alembic/versions/oauth_identity_v1.py tests/test_auth/test_oauth_models.py && git commit -m "feat(scm): users 身份列 + user_scm_token + oauth_state 模型与迁移"
```

---

## Task 4：oauth_state store（mint + 原子一次性消费 + CSRF + GC）

**Files:** Create `src/service/scm/oauth_state_store.py`；Test `tests/test_auth/test_oauth_state_store.py`

> 主键存 `sha256(state)`；行内存 `sha256(csrf)`。消费用 `DELETE … RETURNING`（SQLite/aiosqlite 支持 RETURNING），0 行视为已用/过期。回调先消费再做任何写。

- [ ] **Step 1: 失败测试**
```python
# tests/test_auth/test_oauth_state_store.py
import pytest, pytest_asyncio
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from src.service.db import Base
from src.service.db_models_homepage import OAuthState
from src.service.scm.oauth_state_store import mint_state, consume_state, gc_expired


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_mint_then_consume_once(maker):
    async with maker() as s:
        minted = await mint_state(s, provider="github", purpose="login", user_id=None, with_nonce=True)
        await s.commit()
    # minted.state / minted.csrf 是返给浏览器的明文；DB 存 hash
    async with maker() as s:
        row = await consume_state(s, state=minted.state, csrf=minted.csrf)
        await s.commit()
    assert row is not None and row.provider == "github" and row.purpose == "login" and row.nonce
    # 第二次消费 → None（已删）
    async with maker() as s:
        assert await consume_state(s, state=minted.state, csrf=minted.csrf) is None


@pytest.mark.asyncio
async def test_consume_bad_csrf(maker):
    async with maker() as s:
        minted = await mint_state(s, provider="github", purpose="login", user_id=None, with_nonce=False)
        await s.commit()
    async with maker() as s:
        assert await consume_state(s, state=minted.state, csrf="wrong") is None  # csrf 不符


@pytest.mark.asyncio
async def test_consume_expired(maker):
    async with maker() as s:
        minted = await mint_state(s, provider="github", purpose="login", user_id=None,
                                  with_nonce=False, ttl_seconds=-1)  # 立刻过期
        await s.commit()
    async with maker() as s:
        assert await consume_state(s, state=minted.state, csrf=minted.csrf) is None


@pytest.mark.asyncio
async def test_gc_expired(maker):
    async with maker() as s:
        await mint_state(s, provider="github", purpose="login", user_id=None, with_nonce=False, ttl_seconds=-1)
        await s.commit()
    async with maker() as s:
        n = await gc_expired(s); await s.commit()
    assert n >= 1
```

- [ ] **Step 2: 跑测试确认失败**（ImportError）。

- [ ] **Step 3: 实现**
```python
# src/service/scm/oauth_state_store.py
"""OAuth/OIDC state/nonce 服务端存储：mint（含 CSRF cookie 值）+ 原子一次性消费 + GC。设计 §6 B1/B2/I10。"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.service.db_models_homepage import OAuthState

_DEFAULT_TTL = 600  # 10 分钟


def _sha256(v: str) -> str:
    return hashlib.sha256(v.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MintedState:
    state: str   # 明文，放进 authorize URL 的 state 参数
    csrf: str    # 明文，下发为 httponly cookie ke_oauth_csrf
    nonce: Optional[str]


async def mint_state(session: AsyncSession, *, provider: str, purpose: str,
                     user_id: Optional[int], with_nonce: bool,
                     ttl_seconds: int = _DEFAULT_TTL) -> MintedState:
    """生成 state + csrf（+可选 nonce），DB 只存它们的 sha256。返回明文给调用方下发。"""
    state = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(24) if with_nonce else None
    row = OAuthState(
        state_hash=_sha256(state), csrf_hash=_sha256(csrf),
        provider=provider, purpose=purpose, user_id=user_id, nonce=nonce,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
    )
    session.add(row)
    await session.flush()
    return MintedState(state=state, csrf=csrf, nonce=nonce)


async def consume_state(session: AsyncSession, *, state: str,
                        csrf: Optional[str]) -> Optional[OAuthState]:
    """原子一次性消费：DELETE … RETURNING 拿回行；不存在/已用/过期/CSRF 不符 → None。

    必须在任何 token 交换/身份/token 写入**之前**调用，且与后续写在同一事务（get_db 末统一 commit）。
    """
    if not csrf:
        return None
    sh = _sha256(state)
    # 用 RETURNING 原子取回被删行（aiosqlite/MySQL8 均支持）
    res = await session.execute(
        delete(OAuthState).where(OAuthState.state_hash == sh).returning(OAuthState)
    )
    row = res.scalar_one_or_none()
    if row is None:
        return None
    # 过期
    exp = row.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp < datetime.now(timezone.utc):
        return None
    # CSRF 常数时间比较
    if not hmac.compare_digest(row.csrf_hash, _sha256(csrf)):
        return None
    return row


async def gc_expired(session: AsyncSession) -> int:
    """删除过期 state，返回删除数（回调机会式调用，防无界增长）。"""
    res = await session.execute(
        delete(OAuthState).where(OAuthState.expires_at < datetime.now(timezone.utc))
    )
    return res.rowcount or 0
```
> 注：`consume_state` 即使 CSRF/过期不符也已 DELETE 该行——这是有意的（一次性，失败即作废，防重放）。

- [ ] **Step 4: 跑测试确认通过**（4 passed）。

- [ ] **Step 5: 提交**
```bash
git add src/service/scm/oauth_state_store.py tests/test_auth/test_oauth_state_store.py && git commit -m "feat(scm): oauth_state 存储（mint+原子一次性消费+CSRF 绑定+GC）"
```

---

## Task 5：token store（upsert/取/删 + get_valid_scm_token 刷新）

**Files:** Create `src/service/scm/scm_token_store.py`；Test `tests/test_auth/test_scm_token_store.py`

> Fernet 用 `token_crypto.encrypt_token/decrypt_token`。`get_valid_scm_token`：未过期→直接解密；过期→用 refresh_token 调注入的 `refresh_fn` 换新并 rotation-aware upsert；刷新失败→删 token + 抛 `ScmTokenInvalid`。
> **M5（必读）**：本任务测试触 `token_crypto`，跑测试时**务必带** `KE_TOKEN_ENC_KEY`（合法 Fernet key，见前置 注 1）；裸跑会 `RuntimeError: KE_TOKEN_ENC_KEY 未设置`。`_get_fernet()` 有 `lru_cache`，会话内首次设值即可。

- [ ] **Step 1: 失败测试**
```python
# tests/test_auth/test_scm_token_store.py
import pytest, pytest_asyncio
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select
from src.service.db import Base
from src.service.auth_models import User
from src.service.db_models_homepage import UserScmToken
from src.service.scm.scm_token_store import (
    upsert_token, get_valid_scm_token, delete_token, ScmTokenInvalid,
)


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def _seed_user(maker) -> int:
    async with maker() as s:
        u = User(email="a@x.com", username="alice", hashed_password="h")
        s.add(u); await s.commit()
        return u.id


@pytest.mark.asyncio
async def test_upsert_and_decrypt_roundtrip(maker):
    uid = await _seed_user(maker)
    async with maker() as s:
        await upsert_token(s, user_id=uid, provider="github", access_token="plain-AT",
                           refresh_token=None, expires_at=None, scopes="read:user", scm_login="alice-gh")
        await s.commit()
    async with maker() as s:
        row = (await s.execute(select(UserScmToken).where(UserScmToken.user_id == uid))).scalar_one()
        assert row.access_token != "plain-AT"            # 密文落库
        tok = await get_valid_scm_token(s, user_id=uid, provider="github", refresh_fn=None)
        assert tok == "plain-AT"                          # 未过期直接解密


@pytest.mark.asyncio
async def test_upsert_keeps_linked_at(maker):
    uid = await _seed_user(maker)
    async with maker() as s:
        await upsert_token(s, user_id=uid, provider="github", access_token="a1",
                           refresh_token=None, expires_at=None, scopes=None, scm_login="alice-gh")
        await s.commit()
    async with maker() as s:
        first = (await s.execute(select(UserScmToken).where(UserScmToken.user_id == uid))).scalar_one().linked_at
    async with maker() as s:  # 复写
        await upsert_token(s, user_id=uid, provider="github", access_token="a2",
                           refresh_token=None, expires_at=None, scopes=None, scm_login="alice-gh2")
        await s.commit()
    async with maker() as s:
        row = (await s.execute(select(UserScmToken).where(UserScmToken.user_id == uid))).scalar_one()
        assert row.linked_at == first           # linked_at 不 bump
        assert row.scm_login == "alice-gh2"     # scm_login 刷新


@pytest.mark.asyncio
async def test_get_valid_refreshes_when_expired(maker):
    uid = await _seed_user(maker)
    async with maker() as s:
        await upsert_token(s, user_id=uid, provider="github", access_token="old-AT",
                           refresh_token="RT", expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                           scopes=None, scm_login="alice-gh")
        await s.commit()

    async def refresh_fn(refresh_token):
        assert refresh_token == "RT"
        return {"access_token": "new-AT", "refresh_token": "RT2",
                "expires_at": datetime.now(timezone.utc) + timedelta(hours=1)}

    async with maker() as s:
        tok = await get_valid_scm_token(s, user_id=uid, provider="github", refresh_fn=refresh_fn)
        await s.commit()
        assert tok == "new-AT"
    async with maker() as s:
        row = (await s.execute(select(UserScmToken).where(UserScmToken.user_id == uid))).scalar_one()
        assert (await get_valid_scm_token(s, user_id=uid, provider="github", refresh_fn=None)) == "new-AT"
        # rotation：新 refresh_token 已持久化（解密验证）
        from src.service.token_crypto import decrypt_token
        assert decrypt_token(row.refresh_token) == "RT2"


@pytest.mark.asyncio
async def test_get_valid_invalid_grant_clears(maker):
    uid = await _seed_user(maker)
    async with maker() as s:
        await upsert_token(s, user_id=uid, provider="github", access_token="old",
                           refresh_token="RT", expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                           scopes=None, scm_login="x")
        await s.commit()

    async def refresh_fn(refresh_token):
        raise ScmTokenInvalid("invalid_grant")

    async with maker() as s:
        with pytest.raises(ScmTokenInvalid):
            await get_valid_scm_token(s, user_id=uid, provider="github", refresh_fn=refresh_fn)
        await s.commit()
    async with maker() as s:
        assert (await s.execute(select(UserScmToken).where(UserScmToken.user_id == uid))).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_delete_token(maker):
    uid = await _seed_user(maker)
    async with maker() as s:
        await upsert_token(s, user_id=uid, provider="github", access_token="a",
                           refresh_token=None, expires_at=None, scopes=None, scm_login="x")
        await s.commit()
    async with maker() as s:
        await delete_token(s, user_id=uid, provider="github"); await s.commit()
    async with maker() as s:
        assert (await s.execute(select(UserScmToken).where(UserScmToken.user_id == uid))).scalar_one_or_none() is None
```

- [ ] **Step 2: 跑测试确认失败**（ImportError）。

- [ ] **Step 3: 实现**
```python
# src/service/scm/scm_token_store.py
"""per-user SCM token 存取（Fernet）+ get_valid_scm_token 刷新（rotation-aware，fail-closed）。设计 §4.6/§5。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.service.db_models_homepage import UserScmToken
from src.service.token_crypto import encrypt_token, decrypt_token

# refresh_fn(refresh_token:str) -> dict(access_token, refresh_token?, expires_at?)
RefreshFn = Callable[[str], Awaitable[dict]]


class ScmTokenInvalid(Exception):
    """token 失效且无法刷新（需用户重新关联）。"""


async def upsert_token(session: AsyncSession, *, user_id: int, provider: str,
                       access_token: str, refresh_token: Optional[str],
                       expires_at: Optional[datetime], scopes: Optional[str],
                       scm_login: Optional[str]) -> None:
    """按 (user_id, provider) upsert：access/refresh 加密落库；linked_at 仅首次设、复写不 bump。"""
    row = (await session.execute(
        select(UserScmToken).where(UserScmToken.user_id == user_id, UserScmToken.provider == provider)
    )).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    enc_access = encrypt_token(access_token)
    enc_refresh = encrypt_token(refresh_token) if refresh_token else None
    if row is None:
        session.add(UserScmToken(
            id=f"ust-{uuid.uuid4().hex[:16]}", user_id=user_id, provider=provider,
            access_token=enc_access, refresh_token=enc_refresh, expires_at=expires_at,
            scopes=scopes, scm_login=scm_login, linked_at=now,
        ))
    else:
        row.access_token = enc_access
        if enc_refresh is not None:        # 刷新不返新 refresh 时保留旧的
            row.refresh_token = enc_refresh
        row.expires_at = expires_at
        row.scopes = scopes
        row.scm_login = scm_login
        row.updated_at = now
    await session.flush()


async def get_valid_scm_token(session: AsyncSession, *, user_id: int, provider: str,
                              refresh_fn: Optional[RefreshFn]) -> str:
    """取有效 access token：未过期→解密返回；过期→用 refresh_fn 刷新+rotation upsert；
    无 token / 无法刷新 → ScmTokenInvalid（已清除失效行）。"""
    row = (await session.execute(
        select(UserScmToken).where(UserScmToken.user_id == user_id, UserScmToken.provider == provider)
    )).scalar_one_or_none()
    if row is None:
        raise ScmTokenInvalid(f"{provider} 未关联")
    exp = row.expires_at
    if exp is not None and exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp is None or exp > datetime.now(timezone.utc):
        return decrypt_token(row.access_token)
    # 过期：刷新
    if refresh_fn is None or not row.refresh_token:
        await session.delete(row)
        await session.flush()
        raise ScmTokenInvalid(f"{provider} token 已过期且无法刷新")
    try:
        new = await refresh_fn(decrypt_token(row.refresh_token))
    except ScmTokenInvalid:
        await session.delete(row)
        await session.flush()
        raise
    await upsert_token(
        session, user_id=user_id, provider=provider,
        access_token=new["access_token"], refresh_token=new.get("refresh_token"),
        expires_at=new.get("expires_at"), scopes=row.scopes, scm_login=row.scm_login,
    )
    return new["access_token"]


async def delete_token(session: AsyncSession, *, user_id: int, provider: str) -> None:
    """删除某 provider 的 token 行（解除关联用）。"""
    await session.execute(
        delete(UserScmToken).where(UserScmToken.user_id == user_id, UserScmToken.provider == provider)
    )
    await session.flush()
```

- [ ] **Step 4: 跑测试确认通过**（5 passed）。

- [ ] **Step 5: 提交**
```bash
git add src/service/scm/scm_token_store.py tests/test_auth/test_scm_token_store.py && git commit -m "feat(scm): user_scm_token 存取 + get_valid_scm_token 刷新（rotation/fail-closed）"
```

---

## Task 6：ScmProvider.get_login_identity（Protocol）+ GitHub 登录身份 + OAuth flow

**Files:** Modify `src/service/scm/base.py`、`src/service/scm/github_app.py`；Test `tests/test_auth/test_github_login_identity.py`

> Protocol 加 `get_login_identity`。GitHub：`build_authorize_url(state)`（App user-to-server，不带 scope）+ `exchange_code(code)->dict` + `get_login_identity({"access_token":...})` 调 `GET /user`。

- [ ] **Step 1: 失败测试**（用 pytest-httpx mock `GET /user`）
```python
# tests/test_auth/test_github_login_identity.py
import pytest
from src.service.scm.github_app import GitHubAppProvider
from src.service.scm.config import GitHubAppConfig
from src.service.scm.base import ScmIdentity


def _provider():
    # 登录身份路径不触发 App-JWT；私钥给个最小占位即可（get_login_identity 不签 JWT）
    return GitHubAppProvider(GitHubAppConfig(app_id="1", private_key_pem="x", webhook_secret=""))


def test_build_authorize_url():
    p = _provider()
    url = p.build_authorize_url(client_id="cid", redirect_uri="https://ke/cb", state="st")
    assert "github.com/login/oauth/authorize" in url
    assert "client_id=cid" in url and "state=st" in url and "redirect_uri=" in url


@pytest.mark.asyncio
async def test_get_login_identity(httpx_mock):
    httpx_mock.add_response(url="https://api.github.com/user",
                            json={"id": 42, "login": "octocat"})
    p = _provider()
    ident = await p.get_login_identity({"access_token": "AT"})
    assert ident == ScmIdentity(provider="github", scm_user_id="42", login="octocat")
```

- [ ] **Step 2: 跑测试确认失败**。

- [ ] **Step 3a: Protocol 加方法**（`src/service/scm/base.py` 的 `ScmProvider` 内，追加）
```python
    async def get_login_identity(self, token: dict) -> "ScmIdentity":
        """从本次回调换得的 token 解析登录身份。
        GitHub: token={"access_token": str} → GET /user；GitLab: token={"id_token_claims": dict}。"""
        ...
```

- [ ] **Step 3b: GitHubAppProvider 实现**（`src/service/scm/github_app.py`，类内追加；顶部 import 已有 `httpx`、`_API="https://api.github.com"`）
```python
    def build_authorize_url(self, *, client_id: str, redirect_uri: str, state: str) -> str:
        """GitHub App user-to-server 授权 URL（不带 scope；权限由 App 配置）。"""
        from urllib.parse import urlencode
        q = urlencode({"client_id": client_id, "redirect_uri": redirect_uri, "state": state})
        return f"https://github.com/login/oauth/authorize?{q}"

    async def exchange_code(self, *, client_id: str, client_secret: str,
                            code: str, redirect_uri: str) -> dict:
        """用 code 换 user access token。返回 {"access_token":..., "scope":..., ...}。"""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://github.com/login/oauth/access_token",
                headers={"Accept": "application/json"},
                data={"client_id": client_id, "client_secret": client_secret,
                      "code": code, "redirect_uri": redirect_uri},
            )
            resp.raise_for_status()
            return resp.json()

    async def get_login_identity(self, token: dict) -> "ScmIdentity":
        from src.service.scm.base import ScmIdentity
        access = token["access_token"]
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_API}/user",
                headers={"Authorization": f"token {access}",
                         "Accept": "application/vnd.github+json",
                         "X-GitHub-Api-Version": "2022-11-28"},
            )
            resp.raise_for_status()
            data = resp.json()
        return ScmIdentity(provider="github", scm_user_id=str(data["id"]), login=data.get("login"))
```

- [ ] **Step 4: 跑测试确认通过**（2 passed）。

- [ ] **Step 5: 提交**
```bash
git add src/service/scm/base.py src/service/scm/github_app.py tests/test_auth/test_github_login_identity.py && git commit -m "feat(scm): ScmProvider.get_login_identity + GitHub 登录身份/OAuth flow"
```

---

## Task 7：GitLabOidcProvider（discovery + id_token 完整校验 + 登录身份）

**Files:** Create `src/service/scm/gitlab_oidc.py`；Test `tests/test_auth/test_gitlab_oidc.py`

> id_token 校验：JWKS 验签（alg 锚定 discovery，拒 none/对称）+ iss/aud(+azp)/exp/iat/nonce。用 `joserfc`。

- [ ] **Step 1: 失败测试**（自签 RS256 id_token + mock discovery/JWKS）
```python
# tests/test_auth/test_gitlab_oidc.py
import json, time
import pytest
from joserfc import jwt as jjwt
from joserfc.jwk import RSAKey
from src.service.scm.gitlab_oidc import GitLabOidcProvider
from src.service.scm.config import GitLabOidcConfig
from src.service.scm.base import ScmIdentity

ISS = "https://gitlab.example.com"
AUD = "gl-cid"


def _key():
    return RSAKey.generate_key(2048, parameters={"kid": "k1"})


def _id_token(key, *, iss=ISS, aud=AUD, nonce="N1", exp_delta=600, alg="RS256", sub="sub-9", azp=None):
    now = int(time.time())
    claims = {"iss": iss, "aud": aud, "sub": sub, "exp": now + exp_delta, "iat": now,
              "nonce": nonce, "preferred_username": "gluser"}
    if azp:
        claims["azp"] = azp
    header = {"alg": alg, "kid": "k1"}
    return jjwt.encode(header, claims, key)


def _provider(httpx_mock, key):
    jwks = {"keys": [key.as_dict(private=False)]}
    httpx_mock.add_response(url=f"{ISS}/.well-known/openid-configuration", json={
        "issuer": ISS,
        "authorization_endpoint": f"{ISS}/oauth/authorize",
        "token_endpoint": f"{ISS}/oauth/token",
        "jwks_uri": f"{ISS}/oauth/discovery/keys",
        "id_token_signing_alg_values_supported": ["RS256"],
    })
    httpx_mock.add_response(url=f"{ISS}/oauth/discovery/keys", json=jwks)
    return GitLabOidcProvider(GitLabOidcConfig(issuer=ISS, client_id=AUD, client_secret="s"))


@pytest.mark.asyncio
async def test_validate_id_token_ok(httpx_mock):
    key = _key()
    p = _provider(httpx_mock, key)
    claims = await p.validate_id_token(_id_token(key), expected_nonce="N1")
    assert claims["sub"] == "sub-9"
    ident = await p.get_login_identity({"id_token_claims": claims})
    assert ident == ScmIdentity(provider="gitlab", scm_user_id="sub-9", login="gluser")


@pytest.mark.asyncio
async def test_reject_bad_nonce(httpx_mock):
    key = _key()
    p = _provider(httpx_mock, key)
    with pytest.raises(Exception):
        await p.validate_id_token(_id_token(key, nonce="OTHER"), expected_nonce="N1")


@pytest.mark.asyncio
async def test_reject_bad_iss(httpx_mock):
    key = _key()
    p = _provider(httpx_mock, key)
    with pytest.raises(Exception):
        await p.validate_id_token(_id_token(key, iss="https://evil"), expected_nonce="N1")


@pytest.mark.asyncio
async def test_reject_expired(httpx_mock):
    key = _key()
    p = _provider(httpx_mock, key)
    with pytest.raises(Exception):
        await p.validate_id_token(_id_token(key, exp_delta=-10), expected_nonce="N1")


@pytest.mark.asyncio
async def test_reject_alg_none(httpx_mock):
    key = _key()
    p = _provider(httpx_mock, key)
    # alg=none 的 token（joserfc 编码 none 需特殊 key；直接构造非法签名段亦可触发拒绝）
    bad = _id_token(key).rsplit(".", 1)[0] + "."   # 抹掉签名
    with pytest.raises(Exception):
        await p.validate_id_token(bad, expected_nonce="N1")
```
> 注：`joserfc` 的具体 API 以实现时 venv 内版本为准（`jjwt.encode(header, claims, key)` / `jjwt.decode(token, key_or_keyset, algorithms=[...])`）。若签名 API 略有出入，按已装版本调整测试构造，但**校验项（iss/aud/exp/iat/nonce/alg 锚定）一个不少**。

- [ ] **Step 2: 跑测试确认失败**。

- [ ] **Step 3: 实现**
```python
# src/service/scm/gitlab_oidc.py
"""GitLab OIDC 登录 provider：discovery + 授权 URL + code 交换 + id_token 完整校验 + 登录身份。设计 §4.2/§6 B3。"""
from __future__ import annotations

import time
from typing import Optional
from urllib.parse import urlencode

import httpx
from joserfc import jwt as jjwt
from joserfc.jwk import KeySet

from src.service.scm.base import ScmIdentity
from src.service.scm.config import GitLabOidcConfig

_CLOCK_SKEW = 120  # 秒


class GitLabOidcProvider:
    def __init__(self, cfg: GitLabOidcConfig):
        self._cfg = cfg
        self._disc: Optional[dict] = None

    async def _discovery(self) -> dict:
        """拉 OIDC discovery 文档（含 authorization/token/jwks 端点 + 支持的 alg）。"""
        if self._disc is None:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(f"{self._cfg.issuer}/.well-known/openid-configuration")
                r.raise_for_status()
                self._disc = r.json()
        return self._disc

    async def build_authorize_url(self, *, redirect_uri: str, state: str, nonce: str) -> str:
        disc = await self._discovery()
        q = urlencode({
            "response_type": "code", "client_id": self._cfg.client_id,
            "redirect_uri": redirect_uri, "scope": "openid profile",
            "state": state, "nonce": nonce,
        })
        return f"{disc['authorization_endpoint']}?{q}"

    async def exchange_code(self, *, code: str, redirect_uri: str) -> dict:
        disc = await self._discovery()
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(disc["token_endpoint"], data={
                "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                "client_id": self._cfg.client_id, "client_secret": self._cfg.client_secret,
            }, headers={"Accept": "application/json"})
            r.raise_for_status()
            return r.json()       # 含 access_token / refresh_token / expires_in / id_token

    async def _jwks(self) -> KeySet:
        disc = await self._discovery()
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(disc["jwks_uri"])
            r.raise_for_status()
            return KeySet.import_key_set(r.json())

    async def validate_id_token(self, id_token: str, *, expected_nonce: Optional[str]) -> dict:
        """完整校验 id_token：JWKS 验签 + alg 锚定（拒 none/对称）+ iss/aud/azp/exp/iat/nonce。返回 claims。"""
        disc = await self._discovery()
        allowed = [a for a in disc.get("id_token_signing_alg_values_supported", ["RS256"])
                   if a.startswith(("RS", "ES", "PS"))]   # 仅非对称，杜绝 HS*/none
        if not allowed:
            # ⚠️ 关键：joserfc 的 decode(..., algorithms=[]) 会**放行**任意 alg（空列表=不约束）
            # → 等于关闭验签。空 allowlist 必须直接拒绝。
            raise ValueError("id_token: discovery 未提供可信的非对称签名算法")
        keyset = await self._jwks()
        token = jjwt.decode(id_token, keyset, algorithms=allowed)  # 验签 + alg allowlist（非空）
        claims = token.claims
        now = int(time.time())
        if claims.get("iss") != self._cfg.issuer:
            raise ValueError("id_token iss 不符")
        aud = claims.get("aud")
        aud_ok = (aud == self._cfg.client_id) or (isinstance(aud, list) and self._cfg.client_id in aud)
        if not aud_ok:
            raise ValueError("id_token aud 不符")
        if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != self._cfg.client_id:
            raise ValueError("多 aud 但 azp 不符")
        if int(claims.get("exp", 0)) < now - _CLOCK_SKEW:
            raise ValueError("id_token 已过期")
        if "iat" not in claims or int(claims["iat"]) > now + _CLOCK_SKEW:
            raise ValueError("id_token iat 非法")
        if expected_nonce is not None and claims.get("nonce") != expected_nonce:
            raise ValueError("id_token nonce 不符")
        return dict(claims)

    async def get_login_identity(self, token: dict) -> ScmIdentity:
        claims = token["id_token_claims"]
        return ScmIdentity(provider="gitlab", scm_user_id=str(claims["sub"]),
                           login=claims.get("preferred_username"))
```

- [ ] **Step 4: 跑测试确认通过**（5 passed；若 joserfc API 细节不符按版本微调测试构造，校验项不减）。

- [ ] **Step 5: 提交**
```bash
git add src/service/scm/gitlab_oidc.py tests/test_auth/test_gitlab_oidc.py && git commit -m "feat(scm): GitLabOidcProvider（discovery + id_token 完整校验 + 登录身份）"
```

---

## Task 8：oauth_factory（按 provider 选择，fail-closed）

**Files:** Create `src/service/scm/oauth_factory.py`；Test `tests/test_auth/test_oauth_factory.py`

- [ ] **Step 1: 失败测试**
```python
# tests/test_auth/test_oauth_factory.py
import pytest
from src.service.scm.config import OAuthConfig, GitHubOAuthConfig, GitLabOidcConfig
from src.service.scm.oauth_factory import get_login_provider, OAuthProviderUnavailable
from src.service.scm.github_app import GitHubAppProvider
from src.service.scm.gitlab_oidc import GitLabOidcProvider


def _cfg(github=True, gitlab=True):
    return OAuthConfig(
        redirect_base="https://ke",
        github=GitHubOAuthConfig("gid", "gsec") if github else None,
        gitlab=GitLabOidcConfig("https://gl", "lid", "lsec") if gitlab else None,
    )


def test_github(monkeypatch):
    monkeypatch.setenv("KE_GH_APP_ID", "1")
    monkeypatch.setenv("KE_GH_APP_PRIVATE_KEY_PATH", "/dev/null")
    p = get_login_provider("github", _cfg())
    assert isinstance(p, GitHubAppProvider)


def test_gitlab():
    p = get_login_provider("gitlab", _cfg())
    assert isinstance(p, GitLabOidcProvider)


def test_unconfigured_raises():
    with pytest.raises(OAuthProviderUnavailable):
        get_login_provider("gitlab", _cfg(gitlab=False))


def test_unknown_raises():
    with pytest.raises(OAuthProviderUnavailable):
        get_login_provider("bitbucket", _cfg())
```
> 注：GitHub 分支构造 `GitHubAppProvider` 需 `GitHubAppConfig`（App 私钥）。测试用 `/dev/null` 作私钥路径仅验类型；若 `load_github_app_config` 读 `/dev/null` 得空串仍可构造 provider（不签 JWT）。如该路径校验过严，改用 monkeypatch 写一临时 PEM 文件。

- [ ] **Step 2: 跑测试确认失败**。

- [ ] **Step 3: 实现**
```python
# src/service/scm/oauth_factory.py
"""按 provider 选择登录 provider（fail-closed：未配置 → OAuthProviderUnavailable → 路由 503）。设计 §4.5。"""
from __future__ import annotations

from src.service.scm.config import OAuthConfig, load_github_app_config
from src.service.scm.github_app import GitHubAppProvider
from src.service.scm.gitlab_oidc import GitLabOidcProvider


class OAuthProviderUnavailable(Exception):
    """provider 未配置或不支持（路由层转 503）。"""


def get_login_provider(provider: str, oauth_cfg: OAuthConfig):
    """返回该 provider 的登录 provider 实例。GitHub 复用 GitHubAppProvider（user-token 路径）。"""
    if provider == "github":
        if oauth_cfg.github is None:
            raise OAuthProviderUnavailable("github OAuth 未配置")
        # GitHub 登录路径复用 GitHubAppProvider，需 KE_GH_APP_*；缺失时 load 抛 RuntimeError，
        # 这里转成 OAuthProviderUnavailable，让路由层统一 503（而非 500）。
        try:
            app_cfg = load_github_app_config()
        except RuntimeError as e:
            raise OAuthProviderUnavailable(f"github App 配置缺失：{e}") from e
        return GitHubAppProvider(app_cfg)
    if provider == "gitlab":
        if oauth_cfg.gitlab is None:
            raise OAuthProviderUnavailable("gitlab OIDC 未配置")
        return GitLabOidcProvider(oauth_cfg.gitlab)
    raise OAuthProviderUnavailable(f"不支持的 provider: {provider}")
```

- [ ] **Step 4: 跑测试确认通过**（4 passed）。

- [ ] **Step 5: 提交**
```bash
git add src/service/scm/oauth_factory.py tests/test_auth/test_oauth_factory.py && git commit -m "feat(scm): oauth_factory.get_login_provider（fail-closed）"
```

---

## Task 9：oauth router —— login 入口 + callback 登录分支

**Files:** Create `src/service/scm_oauth_router.py`；Test `tests/test_auth/test_scm_oauth_router.py`

> 本任务只做 `GET /auth/{provider}/login` + `GET /auth/{provider}/callback` 的 **login 分支**（link 分支与 scm-links 在 Task 10）。注入式工厂：`create_scm_oauth_routes(*, get_current_user, get_db, get_login_provider, oauth_config)`。测试注入 fake provider + 直接造 `oauth_state` 行 + mock token 交换。

- [ ] **Step 1: 失败测试**
```python
# tests/test_auth/test_scm_oauth_router.py
import hashlib
from datetime import datetime, timezone, timedelta
import pytest, pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select
from src.service.db import Base
from src.service.auth_models import User
from src.service.db_models_homepage import OAuthState, UserScmToken
from src.service.scm.config import OAuthConfig, GitHubOAuthConfig
from src.service.scm.base import ScmIdentity
from src.service.scm_oauth_router import create_scm_oauth_routes


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


class _FakeGitHub:
    def build_authorize_url(self, *, client_id, redirect_uri, state):
        return f"https://github.com/login/oauth/authorize?state={state}"
    async def exchange_code(self, *, client_id, client_secret, code, redirect_uri):
        return {"access_token": "AT", "scope": "read:user"}
    async def get_login_identity(self, token):
        return ScmIdentity(provider="github", scm_user_id="42", login="octocat")


def _cfg():
    return OAuthConfig(redirect_base="https://ke", github=GitHubOAuthConfig("cid", "sec"), gitlab=None)


def _app(maker, user=None):
    app = FastAPI()
    async def _get_db():
        async with maker() as s:
            yield s
            await s.commit()
    app.include_router(create_scm_oauth_routes(
        get_current_user=(lambda: user), get_db=_get_db,
        get_login_provider=lambda prov, cfg: _FakeGitHub(), oauth_config=_cfg(),
    ))
    return app


@pytest.mark.asyncio
async def test_login_redirects_and_sets_csrf(maker):
    c = TestClient(_app(maker), follow_redirects=False)
    r = c.get("/auth/github/login")
    assert r.status_code in (302, 307)
    assert "github.com/login/oauth/authorize" in r.headers["location"]
    assert "ke_oauth_csrf=" in r.headers.get("set-cookie", "")
    async with maker() as s:
        assert (await s.execute(select(OAuthState))).scalars().first() is not None


@pytest.mark.asyncio
async def test_callback_login_success_issues_tokens(maker, monkeypatch):
    monkeypatch.setenv("KE_JWT_SECRET", "x" * 32)
    # 关联好的用户
    async with maker() as s:
        s.add(User(email="a@x.com", username="alice", hashed_password="h",
                   is_active=True, github_user_id=42)); await s.commit()
    c = TestClient(_app(maker), follow_redirects=False)
    # 走 login 拿 state + csrf cookie
    r0 = c.get("/auth/github/login")
    state = r0.headers["location"].split("state=")[1]
    # 回调（TestClient 自动带上 login 时下发的 csrf cookie）
    r = c.get(f"/auth/github/callback", params={"code": "c", "state": state})
    assert r.status_code in (302, 307)
    # refresh cookie 已下发
    assert "refresh_token=" in r.headers.get("set-cookie", "")
    async with maker() as s:
        assert (await s.execute(select(UserScmToken).where(UserScmToken.user_id != None))).scalars().first() is not None


@pytest.mark.asyncio
async def test_callback_login_unlinked_403(maker, monkeypatch):
    monkeypatch.setenv("KE_JWT_SECRET", "x" * 32)
    c = TestClient(_app(maker), follow_redirects=False)
    r0 = c.get("/auth/github/login")
    state = r0.headers["location"].split("state=")[1]
    r = c.get("/auth/github/callback", params={"code": "c", "state": state})
    assert r.status_code == 403   # 身份未关联任何账号


def test_callback_bad_state_400(maker):
    c = TestClient(_app(maker), follow_redirects=False)
    r = c.get("/auth/github/callback", params={"code": "c", "state": "nope"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_callback_inactive_403(maker, monkeypatch):
    monkeypatch.setenv("KE_JWT_SECRET", "x" * 32)
    async with maker() as s:
        s.add(User(email="d@x.com", username="dan", hashed_password="h",
                   is_active=False, github_user_id=42)); await s.commit()
    c = TestClient(_app(maker), follow_redirects=False)
    r0 = c.get("/auth/github/login")
    state = r0.headers["location"].split("state=")[1]
    r = c.get("/auth/github/callback", params={"code": "c", "state": state})
    assert r.status_code == 403


def test_unknown_provider_404(maker):
    c = TestClient(_app(maker), follow_redirects=False)
    assert c.get("/auth/bitbucket/login").status_code == 404


class _BoomGitHub(_FakeGitHub):
    async def exchange_code(self, *, client_id, client_secret, code, redirect_uri):
        # 模拟上游异常携带敏感串（token/code），断言不外泄
        raise RuntimeError("upstream error token=SECRET_AT code=SECRET_CODE")


def _app_boom(maker):
    app = FastAPI()
    async def _get_db():
        async with maker() as s:
            yield s
            await s.commit()
    app.include_router(create_scm_oauth_routes(
        get_current_user=(lambda: None), get_db=_get_db,
        get_login_provider=lambda prov, cfg: _BoomGitHub(), oauth_config=_cfg()))
    return app


def test_callback_upstream_error_scrubbed(maker, caplog):  # B4
    c = TestClient(_app_boom(maker), follow_redirects=False)
    r0 = c.get("/auth/github/login")
    state = r0.headers["location"].split("state=")[1]
    r = c.get("/auth/github/callback", params={"code": "SECRET_CODE", "state": state})
    assert r.status_code == 502
    # 上游错误体/敏感串绝不透传给客户端
    assert "SECRET_AT" not in r.text and "SECRET_CODE" not in r.text
    # 日志里也不出现 token/code
    assert "SECRET_AT" not in caplog.text and "SECRET_CODE" not in caplog.text


@pytest.mark.asyncio
async def test_callback_fail_closed_on_enc_failure(maker, monkeypatch):  # B5
    import src.service.token_crypto as tc
    monkeypatch.setenv("KE_JWT_SECRET", "x" * 32)
    monkeypatch.delenv("KE_TOKEN_ENC_KEY", raising=False)
    tc.reset_fernet_cache()                       # 让缺 key 立刻生效
    async with maker() as s:
        s.add(User(email="e@x.com", username="eve", hashed_password="h",
                   is_active=True, github_user_id=42)); await s.commit()
    c = TestClient(_app(maker), follow_redirects=False)
    r0 = c.get("/auth/github/login")
    state = r0.headers["location"].split("state=")[1]
    r = c.get("/auth/github/callback", params={"code": "c", "state": state})
    assert r.status_code >= 500                    # 加密失败 → 5xx
    assert "refresh_token=" not in r.headers.get("set-cookie", "")   # 未下发 cookie
    async with maker() as s:
        from src.service.db_models_homepage import UserScmToken
        assert (await s.execute(select(UserScmToken))).scalars().first() is None  # 未写 token（回滚）
```
> 注：`test_callback_*` 依赖全局 `KE_COOKIE_SECURE=false`（见前置 注 2），否则 csrf cookie 被丢→400。`caplog` 是 pytest 内置 fixture，无需额外依赖。

- [ ] **Step 2: 跑测试确认失败**。

- [ ] **Step 3: 实现**（本任务先写 login + callback 的 login 分支；link 分支留 Task 10 的 TODO 占位但**不**报错——见下注释）
```python
# src/service/scm_oauth_router.py
"""SCM OAuth/OIDC 登录 + 关联路由（注入式工厂）。设计 §4.4/§6。"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Callable, Literal, Optional

import src.service.auth_security as sec
from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from src.service.auth_models import User
from src.service.scm.oauth_factory import OAuthProviderUnavailable
from src.service.scm.oauth_state_store import mint_state, consume_state, gc_expired
from src.service.scm.scm_token_store import upsert_token

_log = logging.getLogger("ke.scm.oauth")
_CSRF_COOKIE = "ke_oauth_csrf"
_KNOWN_PROVIDERS = {"github", "gitlab"}
Provider = Literal["github", "gitlab"]   # 仅文档别名；路由参数用 str（见 B1）
# users 列名映射
_ID_COL = {"github": "github_user_id", "gitlab": "gitlab_sub"}


def _id_lookup_value(provider: str, scm_user_id: str):
    """把 ScmIdentity.scm_user_id（字符串）归一成列类型：github→int(BigInteger)，gitlab→str。"""
    return int(scm_user_id) if provider == "github" else scm_user_id


def create_scm_oauth_routes(*, get_current_user: Callable, get_db: Callable,
                            get_login_provider: Callable, oauth_config) -> APIRouter:
    router = APIRouter(tags=["scm-oauth"])

    def _provider_or_503(provider: str):
        # B1：未知 provider → 404（须先于 503 判定；Literal/Enum 路径参数会给 422，故用 str + 显式校验）
        if provider not in _KNOWN_PROVIDERS:
            raise HTTPException(status_code=404, detail=f"未知 provider: {provider}")
        if oauth_config.provider(provider) is None:
            raise HTTPException(status_code=503, detail=f"{provider} 未配置")
        try:
            return get_login_provider(provider, oauth_config)
        except OAuthProviderUnavailable:
            raise HTTPException(status_code=503, detail=f"{provider} 未配置")

    def _redirect_uri(provider: str) -> str:
        # B5：redirect_uri 仅由服务端 redirect_base 派生，绝不取请求 Host/转发头
        return f"{oauth_config.redirect_base}/auth/{provider}/callback"

    def _set_csrf(resp: Response, csrf: str) -> None:
        resp.set_cookie(key=_CSRF_COOKIE, value=csrf, httponly=True,
                        secure=os.getenv("KE_COOKIE_SECURE", "true").lower() == "true",
                        samesite="strict", path="/", max_age=600)

    @router.get("/auth/{provider}/login")
    async def login(provider: str, db=Depends(get_db)):
        prov = _provider_or_503(provider)
        is_oidc = provider == "gitlab"
        minted = await mint_state(db, provider=provider, purpose="login", user_id=None,
                                  with_nonce=is_oidc)
        if provider == "github":
            url = prov.build_authorize_url(
                client_id=oauth_config.github.client_id,
                redirect_uri=_redirect_uri(provider), state=minted.state)
        else:
            url = await prov.build_authorize_url(
                redirect_uri=_redirect_uri(provider), state=minted.state, nonce=minted.nonce)
        resp = RedirectResponse(url, status_code=302)
        _set_csrf(resp, minted.csrf)
        return resp

    async def _resolve_identity(provider: str, prov, code: str, nonce: Optional[str]):
        """换 token + 解析身份（只来自本次 token）。返回 (identity, token_persist)。
        B4：所有上游异常在 callback 里转为通用错误，绝不把 IdP 响应体/token/code 透传或入日志。"""
        if provider == "github":
            tok = await prov.exchange_code(
                client_id=oauth_config.github.client_id,
                client_secret=oauth_config.github.client_secret,
                code=code, redirect_uri=_redirect_uri(provider))
            ident = await prov.get_login_identity({"access_token": tok["access_token"]})
            persist = {"access_token": tok["access_token"], "refresh_token": tok.get("refresh_token"),
                       "expires_at": None, "scopes": tok.get("scope")}
        else:
            tok = await prov.exchange_code(code=code, redirect_uri=_redirect_uri(provider))
            claims = await prov.validate_id_token(tok["id_token"], expected_nonce=nonce)
            ident = await prov.get_login_identity({"id_token_claims": claims})
            exp = None
            if tok.get("expires_in"):
                exp = datetime.now(timezone.utc) + timedelta(seconds=int(tok["expires_in"]))
            persist = {"access_token": tok["access_token"], "refresh_token": tok.get("refresh_token"),
                       "expires_at": exp, "scopes": tok.get("scope")}
        return ident, persist

    @router.get("/auth/{provider}/callback")
    async def callback(provider: str, state: str, code: Optional[str] = None,
                       error: Optional[str] = None, db=Depends(get_db),
                       ke_oauth_csrf: Optional[str] = Cookie(default=None)):
        prov = _provider_or_503(provider)
        # 1) 原子消费 state（先于一切写）；失败/重放/过期/csrf 不符 → 400
        st = await consume_state(db, state=state, csrf=ke_oauth_csrf)
        if st is None:
            raise HTTPException(status_code=400, detail="state 校验失败")
        await gc_expired(db)                       # I2：每次回调机会式清过期 state
        # M6：用户拒授权 / 缺 code → 通用 400（不回显 IdP error 细节，B4）
        if code is None:
            _log.info("oauth callback without code (provider=%s, purpose=%s)", provider, st.purpose)
            raise HTTPException(status_code=400, detail="授权未完成")
        # 2) 换 token + 解析身份；B4：上游异常一律转通用错误，不泄 token/code/IdP 体
        try:
            ident, persist = await _resolve_identity(provider, prov, code, st.nonce)
        except HTTPException:
            raise
        except Exception:                          # noqa: BLE001 — 统一兜成通用错误
            _log.warning("oauth token exchange/identity failed (provider=%s)", provider)
            raise HTTPException(status_code=502, detail="SCM 授权失败")

        if st.purpose == "login":
            col = _ID_COL[provider]
            user = (await db.execute(
                select(User).where(getattr(User, col) == _id_lookup_value(provider, ident.scm_user_id))
            )).scalar_one_or_none()
            if user is None:
                raise HTTPException(status_code=403,
                    detail="该 SCM 身份未关联任何 KE 账号，请先登录并在设置里关联")
            if not user.is_active:
                raise HTTPException(status_code=403, detail="账号已停用")
            if user.locked_until and user.locked_until > datetime.now(timezone.utc):
                raise HTTPException(status_code=423, detail="账号已锁定")
            # B5 fail-closed：先写 token（加密失败→抛→get_db 不 commit→整事务回滚，
            # 不下发 cookie、不签 access），再签 JWT、再建带 cookie 的响应。顺序即保证。
            await upsert_token(db, user_id=user.id, provider=provider,
                               access_token=persist["access_token"], refresh_token=persist["refresh_token"],
                               expires_at=persist["expires_at"], scopes=persist["scopes"],
                               scm_login=ident.login)
            refresh = sec.create_refresh_token(user_id=user.id, remember_me=False)
            resp = RedirectResponse(f"{oauth_config.redirect_base}/", status_code=302)
            resp.set_cookie(value=refresh, **sec.cookie_settings(False))  # access 不进 URL，前端走 /auth/refresh
            return resp
        # link 分支 → Task 10 实现
        raise HTTPException(status_code=400, detail="link 分支未实现（Task 10）")

    return router
```
> 说明：
> - `access_token` 不进 URL；callback 302 到前端首页，前端随后 `POST /auth/refresh` 拿 access（spec §B8）。
> - **B4**：异常一律转通用 `HTTPException`，日志只记 provider/purpose，绝不记 code/state/token；`_resolve_identity` 内不打印任何上游响应。
> - **B5**：`upsert_token`（含 Fernet 加密）在签 JWT/建响应之前；加密失败抛出 → `get_db` 不 commit → 回滚，无 token 行、无 cookie、无 access（fail-closed）。
> - **M2/B5**：不接受任何客户端 `redirect_after`，回跳固定服务端目标，故 open-redirect 白名单按构造即 N/A。
> - **M7（已接受的行为）**：回调失败（403/409/400/502）时 `get_db` 回滚 → state 的 DELETE 也回滚，同一 state 在 TTL（10min）内可重试。鉴于 CSRF cookie 绑定 + 短 TTL + nonce，单次失败可重放视为可接受；fail-closed（不留半写）优先于"失败即消费"。

- [ ] **Step 4: 跑测试确认通过**（8 passed：含 B4 脱敏、B5 fail-closed）。

- [ ] **Step 5: 提交**
```bash
git add src/service/scm_oauth_router.py tests/test_auth/test_scm_oauth_router.py && git commit -m "feat(scm): OAuth login 入口 + callback 登录分支（state/csrf + 双 token + 账号门禁）"
```

---

## Task 10：oauth router —— link 分支 + link-scm start + scm-links GET/DELETE

**Files:** Modify `src/service/scm_oauth_router.py`、`tests/test_auth/test_scm_oauth_router.py`(追加)；Test `tests/test_auth/test_scm_oauth_link.py`

> link 需已登录（`get_current_user`）。link start mint `purpose=link, user_id=当前用户`。callback link 分支写身份列 + token：他人占用→409、自己已绑不同 id→409。scm-links GET/DELETE 纯 DB。

- [ ] **Step 1: 失败测试**
```python
# tests/test_auth/test_scm_oauth_link.py
import pytest, pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select
from src.service.db import Base
from src.service.auth_models import User
from src.service.db_models_homepage import UserScmToken
from src.service.scm.config import OAuthConfig, GitHubOAuthConfig
from src.service.scm.base import ScmIdentity
from src.service.scm_oauth_router import create_scm_oauth_routes


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


class _FakeGitHub:
    def __init__(self, uid="42", login="octocat"):
        self._uid = uid; self._login = login
    def build_authorize_url(self, *, client_id, redirect_uri, state):
        return f"https://gh/auth?state={state}"
    async def exchange_code(self, *, client_id, client_secret, code, redirect_uri):
        return {"access_token": "AT", "scope": "read:user"}
    async def get_login_identity(self, token):
        return ScmIdentity(provider="github", scm_user_id=self._uid, login=self._login)


def _cfg():
    return OAuthConfig(redirect_base="https://ke", github=GitHubOAuthConfig("cid", "sec"), gitlab=None)


def _app(maker, current_user, fake=None):
    app = FastAPI()
    async def _get_db():
        async with maker() as s:
            yield s; await s.commit()
    app.include_router(create_scm_oauth_routes(
        get_current_user=lambda: current_user, get_db=_get_db,
        get_login_provider=lambda prov, cfg: fake or _FakeGitHub(), oauth_config=_cfg()))
    return app


@pytest.mark.asyncio
async def test_link_flow_writes_identity(maker):
    async with maker() as s:
        u = User(email="a@x.com", username="alice", hashed_password="h", is_active=True)
        s.add(u); await s.commit(); uid = u.id
    user = type("U", (), {"id": uid, "username": "alice", "is_admin": False})()
    c = TestClient(_app(maker, user), follow_redirects=False)
    r0 = c.post("/account/link-scm/github/start")
    assert r0.status_code == 200 and "authorize_url" in r0.json()
    state = r0.json()["authorize_url"].split("state=")[1]
    r = c.get("/auth/github/callback", params={"code": "c", "state": state})
    assert r.status_code in (200, 302)
    async with maker() as s:
        row = (await s.execute(select(User).where(User.id == uid))).scalar_one()
        assert row.github_user_id == 42
        assert (await s.execute(select(UserScmToken).where(UserScmToken.user_id == uid))).scalar_one().scm_login == "octocat"


@pytest.mark.asyncio
async def test_link_conflict_other_user_409(maker):
    async with maker() as s:
        other = User(email="o@x.com", username="other", hashed_password="h", github_user_id=42)
        me = User(email="a@x.com", username="alice", hashed_password="h")
        s.add_all([other, me]); await s.commit(); uid = me.id
    user = type("U", (), {"id": uid, "username": "alice", "is_admin": False})()
    c = TestClient(_app(maker, user), follow_redirects=False)
    state = c.post("/account/link-scm/github/start").json()["authorize_url"].split("state=")[1]
    r = c.get("/auth/github/callback", params={"code": "c", "state": state})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_link_self_different_account_409(maker):
    async with maker() as s:
        me = User(email="a@x.com", username="alice", hashed_password="h", github_user_id=99)
        s.add(me); await s.commit(); uid = me.id
    user = type("U", (), {"id": uid, "username": "alice", "is_admin": False})()
    c = TestClient(_app(maker, user, fake=_FakeGitHub(uid="42")), follow_redirects=False)
    state = c.post("/account/link-scm/github/start").json()["authorize_url"].split("state=")[1]
    r = c.get("/auth/github/callback", params={"code": "c", "state": state})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_scm_links_get_and_delete(maker):
    async with maker() as s:
        u = User(email="a@x.com", username="alice", hashed_password="h", github_user_id=42)
        s.add(u); await s.commit(); uid = u.id
        from datetime import datetime
        s.add(UserScmToken(id="t1", user_id=uid, provider="github", access_token="enc",
                           scm_login="octocat", linked_at=datetime(2026, 6, 21))); await s.commit()
    user = type("U", (), {"id": uid, "username": "alice", "is_admin": False})()
    c = TestClient(_app(maker, user), follow_redirects=False)
    links = c.get("/account/scm-links").json()["links"]
    assert any(l["provider"] == "github" and l["scm_login"] == "octocat" for l in links)
    assert c.delete("/account/scm-links/github").status_code == 204
    async with maker() as s:
        assert (await s.execute(select(User).where(User.id == uid))).scalar_one().github_user_id is None
        assert (await s.execute(select(UserScmToken).where(UserScmToken.user_id == uid))).scalar_one_or_none() is None
    # 幂等：再删一次仍 204
    assert c.delete("/account/scm-links/github").status_code == 204
```

- [ ] **Step 2: 跑测试确认失败**。

- [ ] **Step 3: 实现**（替换 Task 9 callback 里的「link 分支未实现」占位 + 加 3 个路由）
在 `_resolve_identity` 与 callback 之间/之后加。先改 callback 的 link 占位为真实分支：
```python
        # --- 替换 callback 末尾的占位 raise，改为 link 分支 ---
        if st.purpose == "link":
            if st.user_id is None:
                raise HTTPException(status_code=400, detail="link state 缺 user_id")
            col = _ID_COL[provider]
            lookup_val = _id_lookup_value(provider, ident.scm_user_id)   # I1：归一列类型再比较
            # 他人占用？
            taken = (await db.execute(
                select(User).where(getattr(User, col) == lookup_val, User.id != st.user_id)
            )).scalar_one_or_none()
            if taken is not None:
                raise HTTPException(status_code=409, detail="该 SCM 身份已被其他账号关联")
            me = (await db.execute(select(User).where(User.id == st.user_id))).scalar_one_or_none()
            if me is None:
                raise HTTPException(status_code=404, detail="用户不存在")
            cur = getattr(me, col)
            if cur is not None and str(cur) != str(ident.scm_user_id):
                raise HTTPException(status_code=409, detail="已关联其他账号，请先解绑")
            setattr(me, col, lookup_val)
            await upsert_token(db, user_id=me.id, provider=provider,
                               access_token=persist["access_token"], refresh_token=persist["refresh_token"],
                               expires_at=persist["expires_at"], scopes=persist["scopes"],
                               scm_login=ident.login)
            return RedirectResponse(f"{oauth_config.redirect_base}/settings", status_code=302)
        raise HTTPException(status_code=400, detail="未知 purpose")
```
追加 3 个路由（`return router` 之前）：
```python
    @router.post("/account/link-scm/{provider}/start")
    async def link_start(provider: str, user=Depends(get_current_user), db=Depends(get_db)):
        prov = _provider_or_503(provider)   # 未知→404、未配→503
        is_oidc = provider == "gitlab"
        minted = await mint_state(db, provider=provider, purpose="link", user_id=user.id,
                                  with_nonce=is_oidc)
        if provider == "github":
            url = prov.build_authorize_url(client_id=oauth_config.github.client_id,
                                           redirect_uri=_redirect_uri(provider), state=minted.state)
        else:
            url = await prov.build_authorize_url(redirect_uri=_redirect_uri(provider),
                                                 state=minted.state, nonce=minted.nonce)
        resp = JSONResponse({"authorize_url": url})
        _set_csrf(resp, minted.csrf)
        return resp

    @router.get("/account/scm-links")
    async def scm_links(user=Depends(get_current_user), db=Depends(get_db)) -> dict:
        rows = (await db.execute(
            select(UserScmToken).where(UserScmToken.user_id == user.id)
        )).scalars().all()
        return {"links": [{"provider": r.provider, "scm_login": r.scm_login,
                           "linked_at": r.linked_at.isoformat() if r.linked_at else None} for r in rows]}

    @router.delete("/account/scm-links/{provider}", status_code=204)
    async def unlink(provider: str, user=Depends(get_current_user), db=Depends(get_db)):
        if provider not in _KNOWN_PROVIDERS:        # B1：未知 provider → 404（不挂 _provider_or_503，解绑不需 provider 配置）
            raise HTTPException(status_code=404, detail=f"未知 provider: {provider}")
        me = (await db.execute(select(User).where(User.id == user.id))).scalar_one_or_none()
        if me is not None:
            setattr(me, _ID_COL[provider], None)
        await delete_token(db, user_id=user.id, provider=provider)
        # I6（已记录决策）：provider 侧 token 撤销本片**有意不做**——解绑只清本地身份+token。
        # KE 是 link-only over 密码账号，解绑不会困住用户；远端撤销留后续 best-effort。
        return Response(status_code=204)
```
顶部 import 处理：
- **B6**：把 Task 9 已加的那行 `from src.service.scm.scm_token_store import upsert_token` **改成** `from src.service.scm.scm_token_store import upsert_token, delete_token`（**改**，不是再加一行）。
- 新增一行 `from fastapi.responses import JSONResponse`（与 Task 9 的 `from fastapi.responses import RedirectResponse` 同区）。

- [ ] **Step 4: 跑测试确认通过**（link 4 passed + Task 9 的 8 passed 不回归）。
Run: `... ./venv/bin/python -m pytest tests/test_auth/test_scm_oauth_router.py tests/test_auth/test_scm_oauth_link.py -v`

- [ ] **Step 5: 提交**
```bash
git add src/service/scm_oauth_router.py tests/test_auth/test_scm_oauth_link.py && git commit -m "feat(scm): OAuth 关联分支 + link-scm start + scm-links 查看/解除"
```

---

## Task 11：挂载 api.py + 全回归

**Files:** Modify `src/service/api.py`

> 生产装配传真实依赖：`get_login_provider`（来自 oauth_factory）+ `load_oauth_config()`。

- [ ] **Step 1: 挂载**（与其它 scm router 一起；顶部已 `import os`、`get_current_user`/`get_db` 已 import）
```python
from src.service.scm_oauth_router import create_scm_oauth_routes
from src.service.scm.oauth_factory import get_login_provider
from src.service.scm.config import load_oauth_config
# ... include_router 区：
app.include_router(create_scm_oauth_routes(
    get_current_user=get_current_user, get_db=get_db,
    get_login_provider=get_login_provider, oauth_config=load_oauth_config(),
))  # P4a：OAuth/OIDC 登录 + 身份关联
```

- [ ] **Step 2: import 冒烟**
Run: `KE_JWT_SECRET=test KE_TOKEN_ENC_KEY=<fernet> ./venv/bin/python -c "import src.service.api"` → 无异常。
> 注：`load_oauth_config()` 在 import 期执行；未配 OAuth env 时返回 provider=None（不抛），故冒烟不需要 OAuth env。

- [ ] **Step 3: P4a 全回归**
Run:
```
KE_JWT_SECRET=test KE_TOKEN_ENC_KEY=<fernet> ./venv/bin/python -m pytest \
  tests/test_auth/test_oauth_config.py tests/test_auth/test_oauth_models.py \
  tests/test_auth/test_oauth_state_store.py tests/test_auth/test_scm_token_store.py \
  tests/test_auth/test_github_login_identity.py tests/test_auth/test_gitlab_oidc.py \
  tests/test_auth/test_oauth_factory.py tests/test_auth/test_scm_oauth_router.py \
  tests/test_auth/test_scm_oauth_link.py -v
```
Expected: 全 PASS。

- [ ] **Step 4: 单一 head + 回归**（用前置的完整 env 前缀，含 `KE_COOKIE_SECURE=false`）
```
... ./venv/bin/alembic heads   # oauth_identity_v1 (head)
... ./venv/bin/python -m pytest tests/test_auth/ -q
```
Expected: 单一 head；test_auth 全绿（仅既有 sqlite teardown 噪声）。
> M1：P4a 改动面在 auth/scm，`tests/test_auth/` 即其 blast radius，作为门禁回归足够。如需更稳，可另跑全库 `./venv/bin/python -m pytest -q`（耗时长，可选）。spec §11#9 的"全量回归"在此口径下=test_auth 全回归 + 下方 import 冒烟。

- [ ] **Step 5: 提交**
```bash
git add src/service/api.py && git commit -m "feat(scm): 挂载 OAuth/OIDC 登录路由（P4a 收尾）"
```

---

## 完成标准（P4a Done，对齐 spec §11 验收）
1. 未关联身份登录 → 403（精确提示）；已关联 → 双 token（refresh cookie 下发）。
2. link 冲突（他人占用 / 自己已绑不同账号）→ 409；解除关联幂等 204 + 清列 + 删 token。
3. provider 未配置 → 503 fail-closed，密码登录不受影响；`{provider}` 未知 → 404。
4. 停用/锁定账号登录 → 403/423。
5. state 重放/过期/缺/错 csrf → 400；同 state 仅一次成功。
6. GitLab id_token 错 nonce/iss/aud/exp/alg → 拒。
7. Fernet token 往返；`get_valid_scm_token` 刷新+rotation 正确、invalid_grant 清除。
8. alembic 单一 head=`oauth_identity_v1`；users 新列 nullable+UNIQUE。
9. test_auth 全回归绿 + `import src.service.api` 冒烟过。

## 待后续（P4b/P4c 衔接）
- P4b：`resolve_scm_role`（经 `get_valid_scm_token` 取 user token）、`authorize()` 交集、bind/QA 门禁、App-install callback 严格归属核验（复用 `oauth_state`+CSRF）、缓存层。
- P4c：`list_user_visible_repos` + `/visible-repos`（∩ list_accessible_projects + Guest-trap）。
