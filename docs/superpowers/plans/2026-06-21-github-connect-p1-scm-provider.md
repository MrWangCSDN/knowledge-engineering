# GitHub 连接 P1：SCM Provider 抽象 + GitHub App 认证 + scm_connection 模型

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 KE 后端能用 GitHub App 认证、列仓、列分支、克隆真实仓库，并落地账号级 `scm_connection` 数据模型——为后续索引作业/连接 API 打底。

**Architecture:** 新增 `src/service/scm/` provider 抽象层（`ScmProvider` Protocol + 数据类 + `ScmRole` 枚举），`GitHubAppProvider` 用 RS256 JWT 换 1h installation token（内存缓存），httpx 调 GitHub REST 列仓/列分支，subprocess 浅克隆。`scm_connection` 表存账号级连接（installation_id / account_login / auth_type）。仅本层 + 模型，不接路由、不接索引（后续 Plan）。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy(async, Mapped) / Alembic / httpx / python-jose[cryptography]（RS256 JWT）/ pytest + pytest-asyncio + pytest-httpx。

**设计依据:** Obsidian `GitHub仓库连接-设计.md` §5.1/§6/§7、`身份与授权模型-设计.md`。

**前置:** 在 worktree `/Users/java/ke-github-connect`（分支 `feat/github-repo-connect`，基于 release-0513）内执行。所有命令用 `./venv/bin/python` / `./venv/bin/pytest`。

---

## 文件结构

| 文件 | 责任 |
|---|---|
| `src/service/scm/__init__.py` | 包入口 |
| `src/service/scm/base.py` | `ScmProvider` Protocol + `RepoInfo`/`BranchInfo`/`BranchList`/`ScmIdentity`/`WebhookEvent` 数据类 + `ScmRole` 枚举 |
| `src/service/scm/config.py` | 从 env 读 GitHub App 配置（app_id / 私钥 / webhook secret） |
| `src/service/scm/github_app.py` | `GitHubAppProvider`：JWT→token、list_repos、list_branches、clone |
| `src/service/db_models_homepage.py`（改） | 新增 `ScmConnection` 模型 |
| `alembic/versions/<id>_scm_connection.py` | 建 `scm_connections` 表 |
| `tests/test_auth/test_scm_connection_model.py` | 模型测试 |
| `tests/test_auth/test_scm_base.py` | 枚举/数据类测试 |
| `tests/test_auth/test_github_app_provider.py` | provider 测试（pytest-httpx mock） |
| `tests/test_auth/test_github_app_clone.py` | clone 命令构造（单元）+ gated 真实克隆 |

---

## Task 1: `scm_connection` 数据模型 + 迁移

**Files:**
- Modify: `src/service/db_models_homepage.py`（在 `GitCredential` 之后新增 `ScmConnection`）
- Create: `alembic/versions/scm_connection_v1_scm_connections.py`
- Test: `tests/test_auth/test_scm_connection_model.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_auth/test_scm_connection_model.py
"""scm_connection 账号级连接模型测试（in-memory SQLite）。"""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select

from src.service.db_models_homepage import Base, ScmConnection


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s


@pytest.mark.asyncio
async def test_scm_connection_roundtrip(session):
    conn = ScmConnection(
        id="conn-1",
        provider="github",
        auth_type="github_app",
        github_installation_id=12345,
        account_login="macrozheng",
        status="active",
        created_by="alice",
    )
    session.add(conn)
    await session.commit()

    row = (await session.execute(select(ScmConnection).where(ScmConnection.id == "conn-1"))).scalar_one()
    assert row.provider == "github"
    assert row.auth_type == "github_app"
    assert row.github_installation_id == 12345
    assert row.account_login == "macrozheng"
    assert row.status == "active"
    assert row.gitlab_instance_url is None  # toB 字段默认空
```

- [ ] **Step 2: 运行测试确认失败**

Run: `./venv/bin/pytest tests/test_auth/test_scm_connection_model.py -v`
Expected: FAIL — `ImportError: cannot import name 'ScmConnection'`

- [ ] **Step 3: 新增模型（最小实现）**

在 `src/service/db_models_homepage.py` 中 `GitCredential` 类之后追加（沿用文件现有 `Mapped`/`mapped_column` 风格与 `Base`/`datetime`/`Optional` 导入）：

```python
class ScmConnection(Base):
    """账号级 SCM 连接（"连接一次"）。设计 GitHub仓库连接-设计.md §5.1。

    github_app：github_installation_id 必填、credential_id 空；
    pat：credential_id 指向 git_credentials、github_installation_id 空。
    """
    __tablename__ = "scm_connections"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # github / gitlab（二期）
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    # github_app / pat
    auth_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # GitHub App 安装 id（App 方式必填）
    github_installation_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    # org/user 名（展示）
    account_login: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # PAT 方式 → git_credentials.id
    credential_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("git_credentials.id", ondelete="SET NULL"), nullable=True
    )
    # toB 自建 GitLab 可配 issuer/base url（二期）
    gitlab_instance_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    oidc_issuer: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    # active / suspended / revoked
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    group_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("groups.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=text("(CURRENT_TIMESTAMP)"), nullable=False
    )
```

如文件顶部未导入 `BigInteger` / `ForeignKey` / `text`，在现有 `from sqlalchemy import ...` 行补齐（`DateTime`/`String` 该文件已用）。

- [ ] **Step 4: 运行测试确认通过**

Run: `./venv/bin/pytest tests/test_auth/test_scm_connection_model.py -v`
Expected: PASS

- [ ] **Step 5: 写 Alembic 迁移**

```python
# alembic/versions/scm_connection_v1_scm_connections.py
"""scm connection v1: scm_connections 表

Revision ID: scm_connection_v1
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "scm_connection_v1"
# 写迁移时 head 为 qa_archive_v1；若已变，先跑 `./venv/bin/alembic heads` 用实际 head
down_revision: Union[str, Sequence[str], None] = "qa_archive_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scm_connections",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("auth_type", sa.String(length=32), nullable=False),
        sa.Column("github_installation_id", sa.BigInteger(), nullable=True),
        sa.Column("account_login", sa.String(length=255), nullable=True),
        sa.Column("credential_id", sa.String(length=64), nullable=True),
        sa.Column("gitlab_instance_url", sa.String(length=512), nullable=True),
        sa.Column("oidc_issuer", sa.String(length=512), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("group_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["credential_id"], ["git_credentials.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="SET NULL"),
    )


def downgrade() -> None:
    op.drop_table("scm_connections")
```

- [ ] **Step 6: 校验迁移链 + 提交**

Run: `./venv/bin/alembic heads` → 确认单一 head = `scm_connection_v1`（无分叉）。
（迁移针对生产 MySQL；单测用 SQLite `create_all`，不跑 alembic。）

```bash
git add src/service/db_models_homepage.py alembic/versions/scm_connection_v1_scm_connections.py tests/test_auth/test_scm_connection_model.py
git commit -m "feat(scm): scm_connections 账号级连接模型 + 迁移"
```

---

## Task 2: SCM 抽象基类 + 数据类 + `ScmRole` 枚举

**Files:**
- Create: `src/service/scm/__init__.py`（空）
- Create: `src/service/scm/base.py`
- Test: `tests/test_auth/test_scm_base.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_auth/test_scm_base.py
"""SCM 抽象层数据类 + 枚举测试。"""
from src.service.scm.base import ScmRole, RepoInfo, BranchList, ScmIdentity


def test_scm_role_enum_values():
    assert {r.value for r in ScmRole} == {"can_bind", "can_query", "not_visible"}


def test_repo_info_fields():
    r = RepoInfo(external_id=42, full_name="macrozheng/mall-swarm", default_branch="master", private=True)
    assert r.external_id == 42
    assert r.full_name == "macrozheng/mall-swarm"
    assert r.private is True


def test_branch_list_default():
    bl = BranchList(default_branch="master", branches=["master", "dev"])
    assert bl.default_branch == "master"
    assert "dev" in bl.branches


def test_scm_identity():
    i = ScmIdentity(provider="github", scm_user_id="100", login="alice")
    assert i.provider == "github"
    assert i.scm_user_id == "100"
```

- [ ] **Step 2: 运行确认失败**

Run: `./venv/bin/pytest tests/test_auth/test_scm_base.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.service.scm'`

- [ ] **Step 3: 实现 base.py**

```python
# src/service/scm/__init__.py
```

```python
# src/service/scm/base.py
"""SCM Provider 抽象：业务层只依赖此处的 Protocol + 数据类 + 枚举，
GitHub/GitLab 各实现一份。设计 GitHub仓库连接-设计.md §6 / 身份与授权模型-设计.md。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Protocol


class ScmRole(str, Enum):
    """SCM 角色翻译到 KE 内部三档枚举（业务层只认这个）。"""
    CAN_BIND = "can_bind"        # 可绑定/连接仓库（owner/maintainer/admin）
    CAN_QUERY = "can_query"      # 可查询问答（read 及以上）
    NOT_VISIBLE = "not_visible"  # 不可见


@dataclass(frozen=True)
class RepoInfo:
    external_id: int          # 仓库 numeric id（绑定主键，rename/transfer 不变）
    full_name: str            # owner/repo（展示）
    default_branch: str
    private: bool = False


@dataclass(frozen=True)
class BranchList:
    default_branch: str
    branches: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ScmIdentity:
    provider: str             # github / gitlab
    scm_user_id: str          # numeric id（GitHub）/ sub（GitLab），账号关联主键
    login: Optional[str] = None


@dataclass(frozen=True)
class WebhookEvent:
    event_type: str           # push / create / installation / ...
    repo_external_id: Optional[int] = None
    ref: Optional[str] = None
    after_sha: Optional[str] = None
    delivery_id: Optional[str] = None


class ScmProvider(Protocol):
    """各 SCM 实现的统一接口。Plan 1 先实 GitHubAppProvider 的列仓/列分支/clone；
    身份与授权方法（get_login_identity/resolve_scm_role/list_user_visible_repos）在 Plan 4。"""

    async def list_repos(self, installation_id: int) -> list[RepoInfo]: ...
    async def list_branches(self, installation_id: int, full_name: str) -> BranchList: ...
    async def clone(self, installation_id: int, full_name: str, ref: str,
                    subpath: Optional[str], dest: str) -> str: ...
```

- [ ] **Step 4: 运行确认通过**

Run: `./venv/bin/pytest tests/test_auth/test_scm_base.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/service/scm/__init__.py src/service/scm/base.py tests/test_auth/test_scm_base.py
git commit -m "feat(scm): ScmProvider 抽象 + 数据类 + ScmRole 枚举"
```

---

## Task 3: GitHub App 配置加载（env）

**Files:**
- Create: `src/service/scm/config.py`
- Test: `tests/test_auth/test_scm_config.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_auth/test_scm_config.py
"""GitHub App 配置从 env 读取测试。"""
import pytest
from src.service.scm.config import load_github_app_config, GitHubAppConfig


def test_load_from_env(monkeypatch, tmp_path):
    pem = tmp_path / "app.pem"
    pem.write_text("-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----\n")
    monkeypatch.setenv("KE_GH_APP_ID", "999")
    monkeypatch.setenv("KE_GH_APP_PRIVATE_KEY_PATH", str(pem))
    monkeypatch.setenv("KE_GH_WEBHOOK_SECRET", "whsec")

    cfg = load_github_app_config()
    assert isinstance(cfg, GitHubAppConfig)
    assert cfg.app_id == "999"
    assert "BEGIN RSA PRIVATE KEY" in cfg.private_key_pem
    assert cfg.webhook_secret == "whsec"


def test_missing_app_id_raises(monkeypatch):
    monkeypatch.delenv("KE_GH_APP_ID", raising=False)
    with pytest.raises(RuntimeError, match="KE_GH_APP_ID"):
        load_github_app_config()
```

- [ ] **Step 2: 运行确认失败**

Run: `./venv/bin/pytest tests/test_auth/test_scm_config.py -v`
Expected: FAIL — `ModuleNotFoundError: ... scm.config`

- [ ] **Step 3: 实现 config.py**

```python
# src/service/scm/config.py
"""GitHub App 配置：私钥从受保护文件读、其余从 env。设计 §7/§11。
绝不硬编码；私钥 PEM 路径走 KE_GH_APP_PRIVATE_KEY_PATH（chmod 600）。"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class GitHubAppConfig:
    app_id: str
    private_key_pem: str
    webhook_secret: str


def load_github_app_config() -> GitHubAppConfig:
    app_id = os.getenv("KE_GH_APP_ID", "").strip()
    if not app_id:
        raise RuntimeError("KE_GH_APP_ID 未设置（GitHub App 连接需要）")
    pem_path = os.getenv("KE_GH_APP_PRIVATE_KEY_PATH", "").strip()
    if not pem_path:
        raise RuntimeError("KE_GH_APP_PRIVATE_KEY_PATH 未设置")
    try:
        with open(pem_path, "r", encoding="utf-8") as f:
            private_key_pem = f.read()
    except OSError as e:
        raise RuntimeError(f"读取 GitHub App 私钥失败（{pem_path}）：{e}") from e
    webhook_secret = os.getenv("KE_GH_WEBHOOK_SECRET", "").strip()
    return GitHubAppConfig(app_id=app_id, private_key_pem=private_key_pem, webhook_secret=webhook_secret)
```

- [ ] **Step 4: 运行确认通过**

Run: `./venv/bin/pytest tests/test_auth/test_scm_config.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: 提交**

```bash
git add src/service/scm/config.py tests/test_auth/test_scm_config.py
git commit -m "feat(scm): GitHub App 配置加载（env + 私钥文件）"
```

---

## Task 4: `GitHubAppProvider` —— JWT 签发 + installation token（带缓存）

**Files:**
- Create: `src/service/scm/github_app.py`
- Test: `tests/test_auth/test_github_app_provider.py`

- [ ] **Step 1: 写失败测试（pytest-httpx mock token 端点）**

```python
# tests/test_auth/test_github_app_provider.py
"""GitHubAppProvider 测试：JWT→installation token + 缓存（pytest-httpx mock）。"""
import time
import pytest

from src.service.scm.config import GitHubAppConfig
from src.service.scm.github_app import GitHubAppProvider

# 测试用 RSA 私钥（仅测试，勿用于生产）—— 2048 位，供 jose RS256 签名
_TEST_PEM = """-----BEGIN PRIVATE KEY-----
MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7VJTUt9Us8cKj
MzEfYyjiWA4R4/M2bS1GB4t7NXp98C3SC6dVMvDuictGeurT8jNbvJZHtCSuYEvu
NMoSfm76oqFvAp8Gy0iz5sxjZmSnXyCdPEovGhLa0VzMaQ8s+CLOyS56YyCFGeJZ
qgtzJ6GR3eqoYSW9b9UMvkBpZODSctWSNGj3P7jRFDO5VoTwCQAWbFnOjDfH5Ulg
p2PKSQnSJP3AJLQNFNe7br1XbrhV//eO+t51mIpGSDCUv3E0DDFcWDTH9cXDTTlR
ZVEiR2BwpZOOkE/Z0/BVnhZYL71oZV34bKfWjQIt6V/isSMahdsAASACp4ZTGtwi
VuNd9tybAgMBAAECggEBAKTmjaS6tkK8BlPXClTQ2vpz/N6uxDeS35mXpqasqskV
laAidgg/sWqpjXDbXr93otIMLlWsM+X0CqMDgSXKejLS2jx4GDjI1ZTXg++0AMJ8
sJ74pWzVDOfmCEQ/7wXs3+cbnXhKriO8Z036q92Qc1+N87SI38nkGa0ABH9CN83H
mQqt4fB7UdHzuIRe/me2PGhIq5ZBzj6h3BpoPGzEP+x3l9YmK8t/1cN0pqI+dQwY
dgfGjackLu/2qH80MCF7IyQaseZUOJyKrCLtSD/Iixv/hzDEUPfOCjFDgTpzf3cw
ta8+oE4wHCo1iI1/4TlPkwmXx4qSXtmw4aQPz7IDQvECgYEA8KNThCO2gsC2I9PQ
DM/8Cw0O983WCDY+oi+7JPiNAJwv5DYBqEZB1QYdj06YD16XlC/HAZMsMku1na2T
N0driwenQQWzoev3g2S7gRDoS/FCJSI3jJ+kjgtaA7Qmzlgk1TxODN+G1H91HW7t
0l7VnL27IWyYo2qRRK3jzxqUiPUCgYEAx0oQs2reBQGMVZnApD1jeq7n4MvNLcPv
t8b/eU9iUv6Y4Mj0Suo/AU8lYZXm8ubbqAlwz2VSVunD2tOplHyMUrtCtObAfVDU
AhCndKaA9gApgfb3xw1IKbuQ1u4IF1FJl3VtumfQn//LiH1B3rXhcdyo3/vIttEk
48RakUKClU8CgYEAzV7W3COOlDDcQd935DdtKBFRAPRPAlspQUnzMi5eSHMD/ISLD
Y5IiQHbIH83D4bvXq0X7qQoSBSNP7Dvv3HYuqMhf0DaegrlBuJllFVVq9qPVRnKx
t1Il2HgxOBvbhOT+9in1BzA+YJ99UzC85O0Qz06A+CmtHEy4aZ2kj5hHjECgYEAm
NS4+A8Fkss8Js1RieK2LniBxMgmYml3pfVLKGnzmng7H2+cwPLhPIzIuwytXywh2b
zbsYEfYx3EoEVgMEpPhoarQnYPukrJO4gwE2o5Te6T5mJSZGlQJQj9q4ZB2Dfzet
6INsK0oG8XVGXSpQvQh3RUYekCZQkBBFcpqWpbIEsCgYAnM3DQf3FJoSnXaMhrVB
Iovic5l0xVvJoSf7T6/oCvzC2hT6f8qg5n3CdmGUkV0sXpQXQYsX36w3K3DXSwO0L
QF8jp7+QzQrECzs5dywGCJqJ0rEQqlS1kEfPa0+TpHEFE4y2k4Jt1MhpkQR9NaPe
qfWUkRBlk0E0XQF0J8t8w==
-----END PRIVATE KEY-----"""


@pytest.fixture
def provider():
    cfg = GitHubAppConfig(app_id="999", private_key_pem=_TEST_PEM, webhook_secret="whsec")
    return GitHubAppProvider(cfg)


def test_app_jwt_is_signed(provider):
    tok = provider._app_jwt()
    assert tok.count(".") == 2  # header.payload.signature


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
    # 第二次命中缓存，不再发请求（httpx_mock 默认断言所有 mock 被用尽；只 add 一个 → 第二次若再请求会报错）
    t2 = await provider.get_installation_token(12345)
    assert t2 == "ghs_abc"
```

- [ ] **Step 2: 运行确认失败**

Run: `./venv/bin/pytest tests/test_auth/test_github_app_provider.py -v`
Expected: FAIL — `ModuleNotFoundError: ... scm.github_app`

- [ ] **Step 3: 实现 github_app.py（JWT + token 缓存）**

```python
# src/service/scm/github_app.py
"""GitHubAppProvider：RS256 JWT 换 1h installation token（内存缓存），httpx 调 REST。
设计 §6/§7。token 不落库，缓存到接近过期重取。"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from jose import jwt

from src.service.scm.config import GitHubAppConfig

_API = "https://api.github.com"
_JWT_TTL = 540          # App JWT 9 分钟（GitHub 上限 10min）
_TOKEN_SKEW = 60        # installation token 提前 60s 视为过期


class GitHubAppProvider:
    def __init__(self, cfg: GitHubAppConfig):
        self._cfg = cfg
        # installation_id -> (token, expires_epoch)
        self._tok_cache: dict[int, tuple[str, float]] = {}

    def _app_jwt(self) -> str:
        """用 App 私钥签 RS256 JWT（iss=app_id），用于换 installation token。"""
        now = int(time.time())
        payload = {"iat": now - 30, "exp": now + _JWT_TTL, "iss": self._cfg.app_id}
        return jwt.encode(payload, self._cfg.private_key_pem, algorithm="RS256")

    async def get_installation_token(self, installation_id: int) -> str:
        """换 1h installation token；缓存命中且未近过期则复用。"""
        cached = self._tok_cache.get(installation_id)
        if cached and cached[1] - _TOKEN_SKEW > time.time():
            return cached[0]
        headers = {
            "Authorization": f"Bearer {self._app_jwt()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_API}/app/installations/{installation_id}/access_tokens", headers=headers
            )
            resp.raise_for_status()
            data = resp.json()
        token = data["token"]
        exp = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
        self._tok_cache[installation_id] = (token, exp)
        return token
```

- [ ] **Step 4: 运行确认通过**

Run: `./venv/bin/pytest tests/test_auth/test_github_app_provider.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: 提交**

```bash
git add src/service/scm/github_app.py tests/test_auth/test_github_app_provider.py
git commit -m "feat(scm): GitHubAppProvider JWT 签发 + installation token 缓存"
```

---

## Task 5: `list_repos` + `list_branches`

**Files:**
- Modify: `src/service/scm/github_app.py`
- Test: `tests/test_auth/test_github_app_provider.py`（追加）

- [ ] **Step 1: 追加失败测试**

```python
# 追加到 tests/test_auth/test_github_app_provider.py
# （沿用文件已有的 provider / httpx_mock fixture，无需新增 import）

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
```

- [ ] **Step 2: 运行确认失败**

Run: `./venv/bin/pytest tests/test_auth/test_github_app_provider.py -k "list_repos or list_branches" -v`
Expected: FAIL — `AttributeError: 'GitHubAppProvider' object has no attribute 'list_repos'`

- [ ] **Step 3: 实现 list_repos + list_branches**

在 `github_app.py` 的 `GitHubAppProvider` 内追加（复用 `get_installation_token` + 一个分页 GET helper）：

```python
    async def _get(self, installation_id: int, path: str) -> httpx.Response:
        token = await self.get_installation_token(installation_id)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{_API}{path}", headers=headers)
            resp.raise_for_status()
            return resp

    async def list_repos(self, installation_id: int) -> list["RepoInfo"]:
        from src.service.scm.base import RepoInfo
        out: list[RepoInfo] = []
        page = 1
        while True:
            resp = await self._get(installation_id, f"/installation/repositories?per_page=100&page={page}")
            repos = resp.json().get("repositories", [])
            if not repos:
                break
            for r in repos:
                out.append(RepoInfo(
                    external_id=r["id"], full_name=r["full_name"],
                    default_branch=r.get("default_branch", "main"), private=bool(r.get("private")),
                ))
            if len(repos) < 100:
                break
            page += 1
        return out

    async def list_branches(self, installation_id: int, full_name: str) -> "BranchList":
        from src.service.scm.base import BranchList
        meta = (await self._get(installation_id, f"/repos/{full_name}")).json()
        default_branch = meta.get("default_branch", "main")
        branches: list[str] = []
        page = 1
        while True:
            resp = await self._get(installation_id, f"/repos/{full_name}/branches?per_page=100&page={page}")
            items = resp.json()
            if not items:
                break
            branches.extend(b["name"] for b in items)
            if len(items) < 100:
                break
            page += 1
        return BranchList(default_branch=default_branch, branches=branches)
```

（顶部 import 也可改为模块级 `from src.service.scm.base import RepoInfo, BranchList`；此处用函数内 import 避免任何潜在循环依赖，二者皆可。）

- [ ] **Step 4: 运行确认通过**

Run: `./venv/bin/pytest tests/test_auth/test_github_app_provider.py -v`
Expected: PASS（全部 passed）

- [ ] **Step 5: 提交**

```bash
git add src/service/scm/github_app.py tests/test_auth/test_github_app_provider.py
git commit -m "feat(scm): GitHubAppProvider list_repos + list_branches"
```

---

## Task 6: `clone`（纯函数构造命令 + gated 真实克隆）

**Files:**
- Modify: `src/service/scm/github_app.py`
- Test: `tests/test_auth/test_github_app_clone.py`

- [ ] **Step 1: 写失败测试（命令构造 = 纯函数，单测；真实克隆 = gated）**

```python
# tests/test_auth/test_github_app_clone.py
"""clone 命令构造（纯函数单测）+ gated 真实浅克隆。"""
import os
import shutil
import tempfile
import pytest

from src.service.scm.github_app import build_clone_args, mask_token


def test_build_clone_args_shallow_branch():
    args = build_clone_args(
        clone_url="https://x-access-token:ghs_abc@github.com/octocat/Hello-World.git",
        ref="master", dest="/tmp/dst",
    )
    assert args[:2] == ["git", "clone"]
    assert "--depth" in args and "1" in args
    assert "--branch" in args and "master" in args
    assert "--single-branch" in args
    assert args[-1] == "/tmp/dst"


def test_mask_token():
    txt = "fatal: auth https://x-access-token:ghs_SECRET@github.com/x.git"
    assert "ghs_SECRET" not in mask_token(txt, "ghs_SECRET")


@pytest.mark.skipif(not os.getenv("KE_GATED_CLONE"), reason="gated：需联网；设 KE_GATED_CLONE=1 启用")
@pytest.mark.asyncio
async def test_real_shallow_clone_public_repo():
    from src.service.scm.github_app import shallow_clone
    dest = tempfile.mkdtemp(prefix="ke-clone-")
    try:
        sha = await shallow_clone(
            "https://github.com/octocat/Hello-World.git", ref="master", dest=dest, token=None
        )
        assert len(sha) == 40
        assert os.path.exists(os.path.join(dest, ".git"))
    finally:
        shutil.rmtree(dest, ignore_errors=True)
```

- [ ] **Step 2: 运行确认失败**

Run: `./venv/bin/pytest tests/test_auth/test_github_app_clone.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_clone_args'`

- [ ] **Step 3: 实现 clone（复用 git_utils 的 subprocess + mask 范式）**

在 `github_app.py` 追加（模块级函数 + provider 方法；`mask_token` 可复用 `src.service.git_utils.mask_token_in_text`，此处独立实现以解耦）：

```python
import asyncio
import re
import subprocess
from typing import Optional


def mask_token(text: str, token: Optional[str]) -> str:
    if not token or not text:
        return text
    return text.replace(token, "***")


def build_clone_args(clone_url: str, ref: str, dest: str) -> list[str]:
    """构造浅克隆命令（单分支）。subpath 的 sparse-checkout 在 shallow_clone 内 clone 后单独执行。"""
    return ["git", "clone", "--depth", "1", "--branch", ref, "--single-branch", clone_url, dest]


def _inject_token(full_name_url: str, token: Optional[str]) -> str:
    if not token:
        return full_name_url
    return full_name_url.replace("https://", f"https://x-access-token:{token}@", 1)


async def _run(args: list[str], token: Optional[str], cwd: Optional[str] = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(mask_token(err.decode("utf-8", "replace"), token))
    return out.decode("utf-8", "replace").strip()


async def shallow_clone(clone_url_https: str, ref: str, dest: str,
                        token: Optional[str], subpath: Optional[str] = None) -> str:
    """浅克隆指定分支到 dest，返回 HEAD commit sha（40 hex）。subpath 非空时 sparse-checkout。"""
    auth_url = _inject_token(clone_url_https, token)
    if subpath:
        # sparse：先 clone（no-checkout）再设 sparse 路径
        await _run(["git", "clone", "--depth", "1", "--branch", ref, "--single-branch",
                    "--filter=blob:none", "--no-checkout", auth_url, dest], token)
        await _run(["git", "sparse-checkout", "set", subpath], token, cwd=dest)
        await _run(["git", "checkout", ref], token, cwd=dest)
    else:
        await _run(build_clone_args(auth_url, ref, dest), token)
    return await _run(["git", "rev-parse", "HEAD"], token, cwd=dest)
```

并给 `GitHubAppProvider` 加 `clone` 方法满足 Protocol：

```python
    async def clone(self, installation_id: int, full_name: str, ref: str,
                    subpath: Optional[str], dest: str) -> str:
        token = await self.get_installation_token(installation_id)
        url = f"https://github.com/{full_name}.git"
        return await shallow_clone(url, ref=ref, dest=dest, token=token, subpath=subpath)
```

- [ ] **Step 4: 运行确认通过（单测）**

Run: `./venv/bin/pytest tests/test_auth/test_github_app_clone.py -v`
Expected: PASS（命令构造 + mask 2 passed，gated 克隆 skipped）

可选联网验证：`KE_GATED_CLONE=1 ./venv/bin/pytest tests/test_auth/test_github_app_clone.py -v`

- [ ] **Step 5: 全量回归 + 提交**

Run: `./venv/bin/pytest tests/test_auth/test_scm_base.py tests/test_auth/test_scm_config.py tests/test_auth/test_scm_connection_model.py tests/test_auth/test_github_app_provider.py tests/test_auth/test_github_app_clone.py -v`
Expected: 全部 PASS（gated 克隆 skipped）

```bash
git add src/service/scm/github_app.py tests/test_auth/test_github_app_clone.py
git commit -m "feat(scm): GitHubAppProvider clone（浅克隆 + sparse 子目录 + token 掩码）"
```

---

## 完成标准（P1 Done）

- `scm_connections` 表 + `ScmConnection` 模型 + 迁移（单一 head）。
- `src/service/scm/`：`base`（Protocol+枚举+数据类）/`config`/`github_app`（JWT→token、list_repos、list_branches、clone）。
- 全部单测绿；gated 真实克隆可选验证。
- **不含**：路由、索引、绑定、身份登录、权限（后续 Plan）。

## 待后续 Plan 衔接

- P2 `index_job` + `ke-indexer`：调 `GitHubAppProvider.clone` 拉代码后跑 pipeline。
- P3 连接 API：用 `list_repos`/`list_branches` + 建 `ScmConnection`。
- P4 身份与授权：给 `ScmProvider` 补 `get_login_identity`/`resolve_scm_role`/`list_user_visible_repos`（base.py 已留位）。
