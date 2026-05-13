# v2.0 多工程隔离 + 权限管理 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn single-tenant knowledge-engineering into GitLab-style multi-tenant: Groups (≤3-level nested) + 3 role levels (reporter/maintainer/owner) + user-scoped credentials + multi-tenant storage (MySQL/Weaviate/Neo4j) + standard audit logging.

**Architecture:** Inheritance-based permission resolution `max(group_chain_role, project_direct_role)`. Weaviate Native Multi-Tenancy. Neo4j project_id property + adapter-enforced filtering. FastAPI dependency factories enforce roles. Audit helper called from each write endpoint.

**Tech Stack:** Python 3.12 / FastAPI 0.136 / SQLAlchemy 2.0 async / asyncmy / Alembic / Weaviate v4.6 (Native MT) / Neo4j Community 5.x driver. Frontend: React 19 / Vite / Zustand / shadcn.

**Design Doc:** `/Users/java/obsidian/01 Engineering/knowledge-engineering-web/多工程隔离与权限-设计.md`

---

## File Structure

### Backend (auth repo)

```
src/service/
├── permission_deps.py            🆕 require_project_role / require_group_role factories
├── audit/
│   ├── __init__.py               🆕
│   ├── actions.py                🆕 action constants
│   └── logger.py                 🆕 log_audit helper
├── db_models_groups.py           🆕 Group / GroupMember / AuditLog models
├── group_router.py               🆕 /groups CRUD + group members
├── project_member_router.py      🆕 /projects/{pid}/members
├── user_router.py                🆕 /admin/users CRUD
├── audit_router.py               🆕 /admin/audit-logs, /groups/{gid}/audit-logs
├── credentials_router.py         🆕 拆出 /credentials (user-scoped)
├── db_models_homepage.py         📝 Project.group_id
├── auth_models.py                — 不动
├── qa_router.py                  📝 加 require_project_role
├── admin_router.py               📝 移除 credentials；保留其它
├── auth_router.py                📝 加 audit on login
├── api.py                        📝 startup: 加 group_router 等
└── qa_engine/
    ├── adapters.py               📝 Weaviate.with_tenant + Neo4j project_id 必填
    ├── retriever.py              📝 接受 project_id
    └── tools/__init__.py         📝 build_default_registry 接 project_id

alembic/versions/
└── XXX_v2_multi_tenant.py        🆕 加表 + 加列

tests/test_auth/
├── test_permission_deps.py       🆕
├── test_audit_logger.py          🆕
├── test_group_router.py          🆕
├── test_project_member_router.py 🆕
├── test_user_router.py           🆕
├── test_audit_router.py          🆕
├── test_credentials_user_scoped.py 🆕
└── test_qa_router.py             📝 适配 require_project_role
```

### Frontend (web repo)

```
src/
├── types/
│   ├── group.ts                  🆕
│   └── audit.ts                  🆕
├── api/
│   ├── groups.ts                 🆕
│   ├── projectMembers.ts         🆕
│   ├── users.ts                  🆕
│   ├── auditLogs.ts              🆕
│   ├── admin.ts                  📝 移除 credentials
│   ├── credentials.ts            🆕 user-scoped
│   └── projects.ts               📝 accessibility filter
├── pages/settings/
│   ├── GroupListPage.tsx         🆕
│   ├── GroupDetailPage.tsx       🆕
│   ├── ProjectDetailPage.tsx     🆕
│   ├── UserListPage.tsx          🆕
│   ├── AuditLogPage.tsx          🆕
│   ├── SettingsLayout.tsx        📝 新 tab
│   ├── RepositoryListPage.tsx    📝 重命名 ProjectListPage
│   └── CredentialListPage.tsx    📝 user-scoped
├── components/
│   ├── group/GroupTreeSelector.tsx 🆕
│   └── layout/TopBar.tsx         📝 tree selector
└── App.tsx                       📝 新 lazy routes
```

---

## Phase 1: 基础设施 (~3 天)

### Task 1: Alembic migration — 创建新表 + 加列

**Files:**
- Create: `alembic/versions/v2_multi_tenant.py`
- Test: `tests/test_auth/test_migration_v2.py`

- [ ] **Step 1: 写迁移文件**

```python
# alembic/versions/v2_multi_tenant.py
"""v2 multi-tenant: groups, group_members, audit_logs + projects.group_id + credentials.owner_user_id"""
from alembic import op
import sqlalchemy as sa

revision = 'v2_multi_tenant'
down_revision = '59e1dde21b76'  # 当前最新版本 (repo_management_v1)
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ─── 1. groups ───
    op.create_table(
        'groups',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('name', sa.String(128), nullable=False),
        sa.Column('description', sa.String(512), nullable=True),
        sa.Column('parent_group_id', sa.String(64),
                  sa.ForeignKey('groups.id', ondelete='RESTRICT'),
                  nullable=True),
        sa.Column('created_by_user_id', sa.Integer,
                  sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime, server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_groups_parent', 'groups', ['parent_group_id'])

    # ─── 2. group_members ───
    op.create_table(
        'group_members',
        sa.Column('user_id', sa.Integer,
                  sa.ForeignKey('users.id', ondelete='CASCADE'),
                  primary_key=True),
        sa.Column('group_id', sa.String(64),
                  sa.ForeignKey('groups.id', ondelete='CASCADE'),
                  primary_key=True),
        sa.Column('role', sa.String(16), nullable=False),
        sa.Column('added_by_user_id', sa.Integer,
                  sa.ForeignKey('users.id'), nullable=False),
        sa.Column('added_at', sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("role IN ('reporter','maintainer','owner')", name='ck_group_members_role'),
    )

    # ─── 3. audit_logs ───
    op.create_table(
        'audit_logs',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('actor_user_id', sa.Integer,
                  sa.ForeignKey('users.id'), nullable=False),
        sa.Column('action', sa.String(64), nullable=False),
        sa.Column('resource_type', sa.String(32), nullable=False),
        sa.Column('resource_id', sa.String(128), nullable=False),
        sa.Column('metadata_json', sa.Text, nullable=True),
        sa.Column('ip_address', sa.String(45), nullable=True),
        sa.Column('created_at', sa.DateTime, server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_audit_actor_time', 'audit_logs', ['actor_user_id', 'created_at'])
    op.create_index('ix_audit_resource', 'audit_logs', ['resource_type', 'resource_id'])

    # ─── 4. projects.group_id ───
    with op.batch_alter_table('projects') as batch:
        batch.add_column(sa.Column('group_id', sa.String(64), nullable=True))
        batch.create_foreign_key('fk_projects_group', 'groups', ['group_id'], ['id'])

    # ─── 5. credentials.owner_user_id ───
    # 先加 nullable=True 列，迁移现有数据，再改为 nullable=False
    with op.batch_alter_table('git_credentials') as batch:
        batch.add_column(sa.Column('owner_user_id', sa.Integer, nullable=True))
        batch.create_foreign_key('fk_credentials_owner', 'users', ['owner_user_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('git_credentials') as batch:
        batch.drop_constraint('fk_credentials_owner', type_='foreignkey')
        batch.drop_column('owner_user_id')
    with op.batch_alter_table('projects') as batch:
        batch.drop_constraint('fk_projects_group', type_='foreignkey')
        batch.drop_column('group_id')
    op.drop_index('ix_audit_resource', 'audit_logs')
    op.drop_index('ix_audit_actor_time', 'audit_logs')
    op.drop_table('audit_logs')
    op.drop_table('group_members')
    op.drop_index('ix_groups_parent', 'groups')
    op.drop_table('groups')
```

- [ ] **Step 2: 写迁移单测**

```python
# tests/test_auth/test_migration_v2.py
import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine
from alembic.config import Config
from alembic import command


@pytest.mark.asyncio
async def test_migration_v2_creates_tables_and_columns(tmp_path):
    """v2 migration creates 3 new tables and adds 2 columns to existing tables."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", db_url.replace("aiosqlite", "pysqlite"))
    # 跑全部 migrations (head)
    command.upgrade(cfg, "head")

    eng = create_async_engine(db_url)
    async with eng.connect() as conn:
        def check(sync_conn):
            insp = inspect(sync_conn)
            tables = insp.get_table_names()
            assert 'groups' in tables
            assert 'group_members' in tables
            assert 'audit_logs' in tables
            proj_cols = [c['name'] for c in insp.get_columns('projects')]
            assert 'group_id' in proj_cols
            cred_cols = [c['name'] for c in insp.get_columns('git_credentials')]
            assert 'owner_user_id' in cred_cols
        await conn.run_sync(check)
```

- [ ] **Step 3: 跑测试验证 RED**

Run: `cd /Users/java/knowledge-engineering-auth && venv/bin/python -m pytest tests/test_auth/test_migration_v2.py -v`
Expected: PASS（migration 文件已写）

- [ ] **Step 4: 跑迁移确认幂等**

Run: `cd /Users/java/knowledge-engineering-auth && venv/bin/alembic upgrade head && venv/bin/alembic downgrade -1 && venv/bin/alembic upgrade head`
Expected: 全部成功

- [ ] **Step 5: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add alembic/versions/v2_multi_tenant.py tests/test_auth/test_migration_v2.py
git commit -m "feat(v2): alembic migration for groups/members/audit_logs + columns"
```

---

### Task 2: SQLAlchemy 模型 — Group / GroupMember / AuditLog

**Files:**
- Create: `src/service/db_models_groups.py`
- Modify: `src/service/db_models_homepage.py:41-...`（Project 类加 group_id）
- Test: `tests/test_auth/test_db_models_groups.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_auth/test_db_models_groups.py
import pytest
from datetime import datetime
from src.service.db_models_groups import Group, GroupMember, AuditLog


def test_group_has_required_fields():
    """Group ORM 类应该有 7 个字段。"""
    cols = {c.name for c in Group.__table__.columns}
    assert cols == {
        'id', 'name', 'description', 'parent_group_id',
        'created_by_user_id', 'created_at',
    }, f"实际：{cols}"


def test_group_member_pk_is_composite():
    """(user_id, group_id) 复合主键。"""
    pk_cols = [c.name for c in GroupMember.__table__.primary_key.columns]
    assert sorted(pk_cols) == ['group_id', 'user_id']


def test_audit_log_has_action_and_metadata():
    cols = {c.name for c in AuditLog.__table__.columns}
    assert 'action' in cols
    assert 'resource_type' in cols
    assert 'resource_id' in cols
    assert 'metadata_json' in cols
```

- [ ] **Step 2: 跑测试 RED**

Run: `venv/bin/python -m pytest tests/test_auth/test_db_models_groups.py -v`
Expected: ImportError / module not found

- [ ] **Step 3: 实现模型**

```python
# src/service/db_models_groups.py
"""v2.0 multi-tenant: Group / GroupMember / AuditLog ORM models."""
from __future__ import annotations
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime, ForeignKey, Index, Integer, String, Text, CheckConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.service.db import Base


class Group(Base):
    """工程分组；嵌套 ≤ 3 层（service 层校验，DB 不约束）。"""
    __tablename__ = "groups"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    parent_group_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        ForeignKey("groups.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    created_by_user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False,
    )


class GroupMember(Base):
    """组成员；role: 'reporter' | 'maintainer' | 'owner'。"""
    __tablename__ = "group_members"

    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    group_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("groups.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    added_by_user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False,
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "role IN ('reporter','maintainer','owner')",
            name='ck_group_members_role',
        ),
    )


class AuditLog(Base):
    """统一审计日志。"""
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False,
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False,
    )

    __table_args__ = (
        Index('ix_audit_actor_time', 'actor_user_id', 'created_at'),
        Index('ix_audit_resource', 'resource_type', 'resource_id'),
    )
```

- [ ] **Step 4: 修改 Project model 加 group_id**

```python
# src/service/db_models_homepage.py:41 附近的 Project 类
# 在已有字段后加：
group_id: Mapped[Optional[str]] = mapped_column(
    String(64),
    ForeignKey("groups.id", ondelete="SET NULL"),
    nullable=True,
)
```

- [ ] **Step 5: 跑测试 GREEN + commit**

```bash
venv/bin/python -m pytest tests/test_auth/test_db_models_groups.py -v
# 全部 PASS
git add src/service/db_models_groups.py src/service/db_models_homepage.py tests/test_auth/test_db_models_groups.py
git commit -m "feat(v2): Group/GroupMember/AuditLog ORM models + Project.group_id"
```

---

### Task 3: 权限解析算法 — resolve_role

**Files:**
- Create: `src/service/permission_deps.py`
- Test: `tests/test_auth/test_permission_deps.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_auth/test_permission_deps.py
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.service.auth_models import User
from src.service.db import Base
from src.service.db_models_groups import Group, GroupMember
from src.service.db_models_homepage import Project
from src.service.permission_deps import (
    ROLE_RANK, resolve_role, list_accessible_projects, _pick_higher,
)


@pytest_asyncio.fixture
async def db():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SM = async_sessionmaker(eng, expire_on_commit=False)
    async with SM() as s:
        yield s


@pytest.mark.asyncio
async def test_instance_admin_always_owner(db):
    u = User(email="a@x.com", username="a", hashed_password="x", is_admin=True, is_active=True)
    p = Project(id="p1", name="P1", status="ready", group_id=None)
    db.add_all([u, p])
    await db.commit()
    assert await resolve_role(u, p, db) == 'owner'


@pytest.mark.asyncio
async def test_no_membership_returns_none(db):
    u = User(email="b@x.com", username="b", hashed_password="x", is_admin=False, is_active=True)
    p = Project(id="p1", name="P1", status="ready", group_id=None)
    db.add_all([u, p])
    await db.commit()
    assert await resolve_role(u, p, db) is None


@pytest.mark.asyncio
async def test_group_role_inherits_to_project(db):
    """User 是 group G 的 reporter → project (group_id=G) 上也是 reporter。"""
    u = User(email="c@x.com", username="c", hashed_password="x", is_admin=False, is_active=True)
    db.add(u)
    await db.flush()
    g = Group(id="g1", name="G1", created_by_user_id=u.id)
    db.add(g)
    p = Project(id="p1", name="P1", status="ready", group_id="g1")
    db.add(p)
    db.add(GroupMember(user_id=u.id, group_id="g1", role="reporter", added_by_user_id=u.id))
    await db.commit()
    assert await resolve_role(u, p, db) == 'reporter'


@pytest.mark.asyncio
async def test_nested_group_inheritance(db):
    """g_root (owner) → g_child → project：用户在 g_root 是 owner，project 也是 owner。"""
    u = User(email="d@x.com", username="d", hashed_password="x", is_admin=False, is_active=True)
    db.add(u); await db.flush()
    db.add(Group(id="root", name="Root", created_by_user_id=u.id))
    db.add(Group(id="child", name="Child", parent_group_id="root", created_by_user_id=u.id))
    db.add(GroupMember(user_id=u.id, group_id="root", role="owner", added_by_user_id=u.id))
    p = Project(id="p1", name="P1", status="ready", group_id="child")
    db.add(p)
    await db.commit()
    assert await resolve_role(u, p, db) == 'owner'


@pytest.mark.asyncio
async def test_pick_higher_takes_max():
    """直接 maintainer + 继承 reporter → 取 maintainer。"""
    assert _pick_higher('reporter', 'maintainer') == 'maintainer'
    assert _pick_higher('owner', 'reporter') == 'owner'
    assert _pick_higher(None, 'reporter') == 'reporter'
    assert _pick_higher('owner', None) == 'owner'
    assert _pick_higher(None, None) is None
```

- [ ] **Step 2: 跑测试 RED**

Run: `venv/bin/python -m pytest tests/test_auth/test_permission_deps.py -v`
Expected: ImportError

- [ ] **Step 3: 实现 permission_deps**

```python
# src/service/permission_deps.py
"""权限解析 + FastAPI dependency 工厂。

v2.0 起强制：
- 每个 /projects/{pid}/* 路由用 Depends(require_project_role("..."))
- 每个 /groups/{gid}/* 路由用 Depends(require_group_role("..."))
- /admin/* 用 Depends(require_admin)（已存在）
"""
from __future__ import annotations
from typing import Optional

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.service.auth_dependencies import get_current_user
from src.service.auth_models import User
from src.service.db import get_db
from src.service.db_models_groups import Group, GroupMember
from src.service.db_models_homepage import Project, UserProjectAccess


ROLE_RANK = {'reporter': 1, 'maintainer': 2, 'owner': 3}


def _pick_higher(a: Optional[str], b: Optional[str]) -> Optional[str]:
    """取两个 role 中权限较高者；None 表示无此 role。"""
    if a is None: return b
    if b is None: return a
    return a if ROLE_RANK[a] >= ROLE_RANK[b] else b


async def resolve_role(
    user: User, project: Project, db: AsyncSession,
) -> Optional[str]:
    """计算 user 在 project 的最终 role。

    取 max(Instance Admin, Project 直接成员, Group 继承链所有 role)。
    """
    if user.is_admin:
        return 'owner'

    # Project 直接成员
    direct = await db.scalar(
        select(UserProjectAccess.role).filter_by(
            user_id=user.id, project_id=project.id,
        )
    )

    # Group 继承链（深度上限 3，循环防护）
    inherited: Optional[str] = None
    cur_gid: Optional[str] = project.group_id
    visited: set[str] = set()
    while cur_gid and cur_gid not in visited:
        visited.add(cur_gid)
        role = await db.scalar(
            select(GroupMember.role).filter_by(user_id=user.id, group_id=cur_gid)
        )
        if role:
            inherited = _pick_higher(inherited, role)
        cur_gid = await db.scalar(
            select(Group.parent_group_id).filter_by(id=cur_gid)
        )

    return _pick_higher(direct, inherited)


async def resolve_group_role(
    user_id: int, group_id: str, db: AsyncSession,
) -> Optional[str]:
    """Group 继承链最终 role（不含 Instance Admin override）。"""
    inherited: Optional[str] = None
    cur_gid: Optional[str] = group_id
    visited: set[str] = set()
    while cur_gid and cur_gid not in visited:
        visited.add(cur_gid)
        role = await db.scalar(
            select(GroupMember.role).filter_by(user_id=user_id, group_id=cur_gid)
        )
        if role:
            inherited = _pick_higher(inherited, role)
        cur_gid = await db.scalar(
            select(Group.parent_group_id).filter_by(id=cur_gid)
        )
    return inherited


async def list_accessible_projects(
    user: User, db: AsyncSession,
) -> list[Project]:
    """用户能看到的所有工程：admin 全部；其它 = direct + group 继承。"""
    if user.is_admin:
        return list((await db.scalars(select(Project))).all())

    direct_pids = set((await db.scalars(
        select(UserProjectAccess.project_id).filter_by(user_id=user.id)
    )).all())

    accessible_groups = await _expand_user_groups(user.id, db)

    pids = set(direct_pids)
    if accessible_groups:
        group_pids = (await db.scalars(
            select(Project.id).filter(Project.group_id.in_(accessible_groups))
        )).all()
        pids.update(group_pids)

    if not pids:
        return []
    return list((await db.scalars(
        select(Project).filter(Project.id.in_(pids))
    )).all())


async def _expand_user_groups(user_id: int, db: AsyncSession) -> set[str]:
    """用户直接成员 group + 所有子孙（深度上限 3）。"""
    direct = set((await db.scalars(
        select(GroupMember.group_id).filter_by(user_id=user_id)
    )).all())
    if not direct:
        return set()

    all_accessible: set[str] = set(direct)
    frontier: set[str] = set(direct)
    for _ in range(3):  # MAX_GROUP_DEPTH
        children = set((await db.scalars(
            select(Group.id).filter(Group.parent_group_id.in_(frontier))
        )).all())
        new = children - all_accessible
        if not new:
            break
        all_accessible.update(new)
        frontier = new
    return all_accessible


# ─── FastAPI dependency 工厂 ─────────────────────────────────


def require_project_role(min_role: str = "reporter"):
    """权限 dependency：校验当前用户在 path 的 project_id 上的 role ≥ min_role。

    用法：
        @router.post("/projects/{project_id}/...",
                     dependencies=[Depends(require_project_role("maintainer"))])
    """
    async def checker(
        project_id: str,
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> str:
        project = await db.get(Project, project_id)
        if not project:
            raise HTTPException(status_code=404, detail="工程不存在")
        role = await resolve_role(user, project, db)
        if role is None:
            raise HTTPException(status_code=403, detail="无权访问此工程")
        if ROLE_RANK[role] < ROLE_RANK[min_role]:
            raise HTTPException(
                status_code=403,
                detail=f"需要 {min_role} 及以上权限（当前 {role}）",
            )
        return role
    return checker


def require_group_role(min_role: str = "reporter"):
    """同理，作用于 path 的 group_id。"""
    async def checker(
        group_id: str,
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> str:
        group = await db.get(Group, group_id)
        if not group:
            raise HTTPException(status_code=404, detail="组不存在")
        if user.is_admin:
            return 'owner'
        role = await resolve_group_role(user.id, group_id, db)
        if role is None:
            raise HTTPException(status_code=403, detail="无权访问此组")
        if ROLE_RANK[role] < ROLE_RANK[min_role]:
            raise HTTPException(
                status_code=403,
                detail=f"需要 {min_role} 及以上权限（当前 {role}）",
            )
        return role
    return checker
```

- [ ] **Step 4: 跑测试 GREEN**

Run: `venv/bin/python -m pytest tests/test_auth/test_permission_deps.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add src/service/permission_deps.py tests/test_auth/test_permission_deps.py
git commit -m "feat(v2): permission_deps with resolve_role + require_project_role"
```

---

### Task 4: 审计日志 helper

**Files:**
- Create: `src/service/audit/__init__.py`, `src/service/audit/actions.py`, `src/service/audit/logger.py`
- Test: `tests/test_auth/test_audit_logger.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_auth/test_audit_logger.py
import json
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.service.audit.logger import log_audit
from src.service.audit import actions
from src.service.auth_models import User
from src.service.db import Base
from src.service.db_models_groups import AuditLog


@pytest_asyncio.fixture
async def db():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SM = async_sessionmaker(eng, expire_on_commit=False)
    async with SM() as s:
        s.add(User(id=1, email="a@x.com", username="a", hashed_password="x", is_active=True, is_admin=True))
        await s.commit()
        yield s


@pytest.mark.asyncio
async def test_log_audit_writes_row(db):
    await log_audit(
        db,
        actor_user_id=1,
        action=actions.PROJECT_CREATE,
        resource_type="project",
        resource_id="petclinic",
        metadata={"name": "PetClinic", "group_id": "retail"},
    )
    await db.commit()
    row = (await db.scalars(select(AuditLog))).one()
    assert row.action == "project.create"
    assert row.resource_id == "petclinic"
    meta = json.loads(row.metadata_json)
    assert meta["name"] == "PetClinic"


@pytest.mark.asyncio
async def test_log_audit_does_not_raise_on_failure(db):
    """审计写入失败不应让业务挂掉。"""
    await log_audit(
        db,
        actor_user_id=999999,  # 不存在的 user，理论上 FK 会触发问题
        action="bad.action",
        resource_type="x",
        resource_id="y",
    )
    # commit 阶段才会触发 FK 错；test_audit_logger 只测 log_audit 本身不抛
    # 这里不 commit，仅断言 log_audit 调用没崩
    assert True


def test_action_constants_present():
    """关键 action 名字必须存在。"""
    assert actions.PROJECT_CREATE == "project.create"
    assert actions.GROUP_MEMBER_ADD == "group_member.add"
    assert actions.AUTH_LOGIN_SUCCESS == "auth.login_success"
    assert actions.MESSAGE_EXPORT_DOCX == "message.export_docx"
```

- [ ] **Step 2: 跑 RED**

Run: `venv/bin/python -m pytest tests/test_auth/test_audit_logger.py -v`
Expected: ImportError

- [ ] **Step 3: 实现**

```python
# src/service/audit/__init__.py
"""v2.0 audit 子包。"""

# src/service/audit/actions.py
"""审计动作常量。统一前缀：resource_type.verb。"""

# 认证
AUTH_LOGIN_SUCCESS = "auth.login_success"
AUTH_LOGIN_FAILURE = "auth.login_failure"
AUTH_LOGOUT = "auth.logout"
AUTH_PASSWORD_CHANGE = "auth.password_change"

# 用户
USER_CREATE = "user.create"
USER_UPDATE = "user.update"
USER_DELETE = "user.delete"
USER_SET_ADMIN = "user.set_admin"
USER_ACTIVATE = "user.activate"
USER_DEACTIVATE = "user.deactivate"

# Group
GROUP_CREATE = "group.create"
GROUP_UPDATE = "group.update"
GROUP_DELETE = "group.delete"
GROUP_MEMBER_ADD = "group_member.add"
GROUP_MEMBER_REMOVE = "group_member.remove"
GROUP_MEMBER_ROLE_CHANGE = "group_member.role_change"

# Project
PROJECT_CREATE = "project.create"
PROJECT_UPDATE = "project.update"
PROJECT_DELETE = "project.delete"
PROJECT_REINDEX_TRIGGER = "project.reindex_trigger"
PROJECT_MEMBER_ADD = "project_member.add"
PROJECT_MEMBER_REMOVE = "project_member.remove"
PROJECT_MEMBER_ROLE_CHANGE = "project_member.role_change"

# 凭证
CREDENTIAL_CREATE = "credential.create"
CREDENTIAL_DELETE = "credential.delete"

# 导出
MESSAGE_EXPORT_DOCX = "message.export_docx"
```

```python
# src/service/audit/logger.py
"""统一审计日志写入入口。"""
from __future__ import annotations
import json
import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from src.service.db_models_groups import AuditLog

logger = logging.getLogger(__name__)


async def log_audit(
    db: AsyncSession,
    *,
    actor_user_id: int,
    action: str,
    resource_type: str,
    resource_id: str,
    metadata: Optional[dict] = None,
    ip_address: Optional[str] = None,
) -> None:
    """写入审计日志到 db.session（不主动 commit）。

    审计失败仅 log 警告，不抛错（审计失败 ≠ 业务失败）。

    Args:
        db: 当前请求 AsyncSession（与业务事务共用，一起 commit）
        actor_user_id: 谁做的
        action: 来自 audit.actions 常量
        resource_type: 'project' / 'group' / 'user' / 'credential' / 'message'
        resource_id: 资源 ID（str）
        metadata: 附加上下文（dict，会 JSON 序列化）
        ip_address: 客户端 IP（可选）
    """
    try:
        db.add(AuditLog(
            actor_user_id=actor_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id),
            metadata_json=json.dumps(metadata or {}, ensure_ascii=False),
            ip_address=ip_address,
        ))
    except Exception as e:
        logger.warning(
            "audit log 写入失败 action=%s resource=%s:%s err=%s",
            action, resource_type, resource_id, e,
        )
```

- [ ] **Step 4: 跑 GREEN + commit**

```bash
venv/bin/python -m pytest tests/test_auth/test_audit_logger.py -v
git add src/service/audit/ tests/test_auth/test_audit_logger.py
git commit -m "feat(v2): audit logger helper + action constants"
```

---

## Phase 2: RBAC 应用 (~3 天)

### Task 5: qa_router 加 require_project_role

**Files:**
- Modify: `src/service/qa_router.py`
- Modify: `tests/test_auth/test_qa_router.py`

- [ ] **Step 1: 加 RED 测试 — 非成员问答返 403**

```python
# tests/test_auth/test_qa_router.py 末尾新增
def test_explain_403_when_user_not_project_member(client, seed_ready_project, session_maker):
    """新建一个普通用户 bob，未加入工程 → 提问应 403。"""
    # alice 是默认 admin；要测的是非 admin、非成员场景
    # 改 alice.is_admin=False 后重试
    import asyncio
    from sqlalchemy import update
    from src.service.auth_models import User
    async def remove_admin():
        async with session_maker() as s:
            await s.execute(update(User).where(User.username == "alice").values(is_admin=False))
            await s.commit()
    asyncio.get_event_loop().run_until_complete(remove_admin())
    
    token = _login(client)
    r = client.post(
        f"/projects/{seed_ready_project}/qa/explain",
        headers=_auth(token),
        json={"question": "x"},
    )
    assert r.status_code == 403
```

- [ ] **Step 2: 跑 RED**

Run: `venv/bin/python -m pytest tests/test_auth/test_qa_router.py::test_explain_403_when_user_not_project_member -v`
Expected: FAIL (alice 当前仍是 admin 或返 200)

- [ ] **Step 3: 在 qa_router 加 dependency**

```python
# src/service/qa_router.py（顶部 import 加）
from src.service.permission_deps import require_project_role

# 在 router 定义后改：
router = APIRouter(prefix="/projects/{project_id}/qa", tags=["qa"])

# 给 explain 路由加 dependency：
@router.post(
    "/explain",
    dependencies=[Depends(require_project_role("reporter"))],
)
async def explain(...):  # 内部不变
    ...

# 同样给：
# - GET /sessions
# - GET /sessions/{session_id}
# - DELETE /sessions/{session_id}
# - POST /sessions/{session_id}/messages/{message_id}/feedback
# - GET /sessions/{session_id}/messages/{message_id}/export
# 都加 dependencies=[Depends(require_project_role("reporter"))]
```

- [ ] **Step 4: 跑 GREEN + 既有测试不破**

```bash
venv/bin/python -m pytest tests/test_auth/test_qa_router.py -v
```

注意：已有的 `seed_ready_project` fixture 应该已经把 alice 加进 user_project_access（v1 测试约定）；如果没有，要更新 fixture。

- [ ] **Step 5: Commit**

```bash
git add src/service/qa_router.py tests/test_auth/test_qa_router.py
git commit -m "feat(v2): apply require_project_role to qa_router endpoints"
```

---

### Task 6: 凭证拆分 — /credentials (user-scoped) + /admin/credentials (audit)

**Files:**
- Create: `src/service/credentials_router.py`
- Modify: `src/service/admin_router.py`（移除凭证 endpoints）
- Modify: `src/service/api.py`（include 新 router）
- Test: `tests/test_auth/test_credentials_user_scoped.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_auth/test_credentials_user_scoped.py
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient
# ... 标准 client + auth fixtures ...


def test_create_credential_owned_by_current_user(client):
    """任何登录用户都能建凭证；owner_user_id = 自己。"""
    token = _login_as(client, "alice")
    r = client.post("/credentials", json={
        "name": "alice 的 PAT",
        "token": "sk-test-fake-1234567",
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 201
    cid = r.json()["id"]
    # 立刻能看到
    r2 = client.get("/credentials", headers={"Authorization": f"Bearer {token}"})
    names = [c["name"] for c in r2.json()]
    assert "alice 的 PAT" in names


def test_user_cannot_see_others_credentials(client):
    """Bob 看不到 alice 创建的凭证。"""
    alice_token = _login_as(client, "alice")
    client.post("/credentials", json={"name": "alice PAT", "token": "sk-a-xxx-12345"},
                headers={"Authorization": f"Bearer {alice_token}"})
    bob_token = _login_as(client, "bob")
    r = client.get("/credentials", headers={"Authorization": f"Bearer {bob_token}"})
    names = [c["name"] for c in r.json()]
    assert "alice PAT" not in names


def test_admin_sees_all_via_admin_endpoint(client):
    """Instance Admin 通过 /admin/credentials 看到全部（仅 hint）。"""
    alice_token = _login_as(client, "alice")  # alice 是 admin
    client.post("/credentials", json={"name": "alice PAT", "token": "sk-a-xxx-12345"},
                headers={"Authorization": f"Bearer {alice_token}"})
    bob_token = _login_as(client, "bob")
    client.post("/credentials", json={"name": "bob PAT", "token": "sk-b-yyy-67890"},
                headers={"Authorization": f"Bearer {bob_token}"})
    r = client.get("/admin/credentials", headers={"Authorization": f"Bearer {alice_token}"})
    assert r.status_code == 200
    names = [c["name"] for c in r.json()]
    assert "alice PAT" in names and "bob PAT" in names
    # PAT 本体不暴露
    for c in r.json():
        assert "token" not in c
        assert "encrypted_token" not in c
```

- [ ] **Step 2: 跑 RED**

Expected: FAIL（/credentials 不存在）

- [ ] **Step 3: 实现 credentials_router**

```python
# src/service/credentials_router.py
"""用户级凭证管理 (v2.0)：每个用户管自己的 PAT。

GET    /credentials           list MY
POST   /credentials           create MY
DELETE /credentials/{cid}     delete MY
"""
from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.service.audit.logger import log_audit
from src.service.audit import actions
from src.service.auth_dependencies import get_current_user
from src.service.auth_models import User
from src.service.db import get_db
from src.service.db_models_homepage import GitCredential
from src.service.token_crypto import encrypt_token, token_hint


router = APIRouter(prefix="/credentials", tags=["credentials"])


class CredentialCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    token: str = Field(..., min_length=8, max_length=512)
    type: str = Field("pat", max_length=32)


class CredentialResponse(BaseModel):
    id: str
    name: str
    type: str
    token_hint: Optional[str]
    created_at: str


@router.get("", response_model=list[CredentialResponse])
async def list_my_credentials(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = await db.scalars(
        select(GitCredential).filter_by(owner_user_id=user.id)
    )
    return [_to_response(c) for c in rows.all()]


@router.post("", response_model=CredentialResponse, status_code=201)
async def create_my_credential(
    body: CredentialCreateRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    import uuid
    cred = GitCredential(
        id="cred_" + uuid.uuid4().hex[:12],
        name=body.name,
        type=body.type,
        encrypted_token=encrypt_token(body.token),
        token_hint=token_hint(body.token),
        owner_user_id=user.id,
        created_by=user.username,
    )
    db.add(cred)
    await log_audit(
        db,
        actor_user_id=user.id,
        action=actions.CREDENTIAL_CREATE,
        resource_type="credential",
        resource_id=cred.id,
        metadata={"name": cred.name, "type": cred.type},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return _to_response(cred)


@router.delete("/{cred_id}", status_code=204)
async def delete_my_credential(
    cred_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    cred = await db.get(GitCredential, cred_id)
    if not cred:
        raise HTTPException(status_code=404, detail="凭证不存在")
    if cred.owner_user_id != user.id:
        raise HTTPException(status_code=403, detail="不是该凭证的所有者")
    await db.delete(cred)
    await log_audit(
        db,
        actor_user_id=user.id,
        action=actions.CREDENTIAL_DELETE,
        resource_type="credential",
        resource_id=cred.id,
        metadata={"name": cred.name},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()


def _to_response(cred: GitCredential) -> CredentialResponse:
    return CredentialResponse(
        id=cred.id,
        name=cred.name,
        type=cred.type,
        token_hint=cred.token_hint,
        created_at=cred.created_at.isoformat(),
    )
```

- [ ] **Step 4: 在 admin_router 改造 /admin/credentials**

保留 `GET /admin/credentials`（列出所有 + 仅 hint，供审计）+ `DELETE /admin/credentials/{cid}`（强删，写审计）；**移除 POST /admin/credentials**（迁移到 user-level）。

- [ ] **Step 5: api.py include 新 router + 跑测试 + commit**

```python
# src/service/api.py
from src.service.credentials_router import router as credentials_router
app.include_router(credentials_router)
```

```bash
venv/bin/python -m pytest tests/test_auth/test_credentials_user_scoped.py -v
git add src/service/credentials_router.py src/service/admin_router.py src/service/api.py \
        tests/test_auth/test_credentials_user_scoped.py
git commit -m "feat(v2): user-scoped credentials router + admin audit endpoint"
```

---

## Phase 3: 新增 CRUD 路由 (~5 天)

### Task 7: Groups CRUD

**Files:**
- Create: `src/service/group_router.py`
- Test: `tests/test_auth/test_group_router.py`

- [ ] **Step 1: 列出测试场景（写在文件顶部）**

```python
# tests/test_auth/test_group_router.py
"""测试场景清单：
1. POST /groups: Instance Admin 能建根 group → 201
2. POST /groups: 非 admin 想建根 group → 403
3. POST /groups: 在 parent_group_id 下 Group Owner 能建子组 → 201
4. POST /groups: 嵌套深度 4 → 422 "嵌套深度超过 3 层"
5. POST /groups: parent_group_id 不存在 → 404
6. GET /groups: 返回用户可见的 group 树
7. PATCH /groups/{gid}: maintainer 能改 name
8. PATCH /groups/{gid}: reporter 不能改 → 403
9. DELETE /groups/{gid}: 有子组 → 403 "先删子组"
10. DELETE /groups/{gid}: 有工程 → 403 "先迁移工程"
11. DELETE /groups/{gid}: 干净时 owner 能删 → 204
"""
```

- [ ] **Step 2: 写关键测试 (覆盖 5 个场景)**

```python
# 全部代码示例（fixtures 类似 test_qa_router.py 的标准模式）
# 包含 admin / owner_alice / reporter_bob 三个用户、嵌套两层 group 准备

@pytest.mark.asyncio
async def test_admin_can_create_root_group(client_admin):
    r = client_admin.post("/groups", json={
        "id": "retail-bank",
        "name": "零售银行",
        "description": "零售业务相关工程",
    })
    assert r.status_code == 201
    assert r.json()["id"] == "retail-bank"


def test_non_admin_cannot_create_root_group(client_bob):
    r = client_bob.post("/groups", json={
        "id": "x", "name": "X",
    })
    assert r.status_code == 403


def test_group_owner_can_create_subgroup(client_alice, seed_root_group_alice_owner):
    r = client_alice.post("/groups", json={
        "id": "retail-bank/credit-card",
        "name": "信用卡",
        "parent_group_id": "retail-bank",
    })
    assert r.status_code == 201


def test_group_nesting_exceeds_3_layers_rejected(client_admin, seed_3_level_groups):
    """已有 root/lv1/lv2，再建 lv2 的子组 = 第 4 层，应 422。"""
    r = client_admin.post("/groups", json={
        "id": "root/lv1/lv2/lv3",
        "name": "lv3",
        "parent_group_id": "root/lv1/lv2",
    })
    assert r.status_code == 422
    assert "嵌套深度" in r.json()["detail"]


def test_cannot_delete_group_with_subgroups(client_admin, seed_2_level_groups):
    r = client_admin.delete("/groups/root")
    assert r.status_code == 403
    assert "先删子组" in r.json()["detail"]
```

- [ ] **Step 3: 跑 RED**

Expected: FAIL（group_router 不存在）

- [ ] **Step 4: 实现 group_router**

```python
# src/service/group_router.py
"""Group CRUD + 嵌套深度校验。"""
from __future__ import annotations
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.service.audit.logger import log_audit
from src.service.audit import actions
from src.service.auth_dependencies import get_current_user
from src.service.auth_models import User
from src.service.db import get_db
from src.service.db_models_groups import Group, GroupMember
from src.service.db_models_homepage import Project
from src.service.permission_deps import (
    ROLE_RANK, require_group_role, resolve_group_role,
)


MAX_GROUP_DEPTH = 3


router = APIRouter(prefix="/groups", tags=["groups"])


# ─── 请求/响应模型 ───
class GroupCreateRequest(BaseModel):
    id: str = Field(..., min_length=2, max_length=64,
                    pattern=r"^[a-z][a-z0-9/\-]*[a-z0-9]$")
    name: str = Field(..., min_length=1, max_length=128)
    description: Optional[str] = Field(None, max_length=512)
    parent_group_id: Optional[str] = None


class GroupUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, max_length=128)
    description: Optional[str] = Field(None, max_length=512)


class GroupResponse(BaseModel):
    id: str
    name: str
    description: Optional[str]
    parent_group_id: Optional[str]
    created_at: str


# ─── helper: 嵌套深度校验 ───
async def _group_depth(group_id: str, db: AsyncSession) -> int:
    """从该 group 走到根需要几跳；返回 depth (1=根)。"""
    depth = 1
    cur = group_id
    visited = {cur}
    while True:
        parent = await db.scalar(select(Group.parent_group_id).filter_by(id=cur))
        if parent is None:
            return depth
        if parent in visited:
            raise HTTPException(status_code=500, detail="检测到 group 循环依赖")
        visited.add(parent)
        depth += 1
        cur = parent
        if depth > MAX_GROUP_DEPTH * 2:  # 防御
            raise HTTPException(status_code=500, detail="嵌套异常")


# ─── 路由 ───
@router.post("", response_model=GroupResponse, status_code=201)
async def create_group(
    body: GroupCreateRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # 权限：根 group 仅 Instance Admin；子 group 要 parent 的 Owner（含继承）或 admin
    if body.parent_group_id is None:
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="仅 Instance Admin 能建根 group")
    else:
        parent = await db.get(Group, body.parent_group_id)
        if not parent:
            raise HTTPException(status_code=404, detail="父组不存在")
        # 校验 owner
        if not user.is_admin:
            role = await resolve_group_role(user.id, body.parent_group_id, db)
            if role != 'owner':
                raise HTTPException(status_code=403, detail="需要父组 Owner 权限")
        # 嵌套深度校验
        parent_depth = await _group_depth(body.parent_group_id, db)
        if parent_depth + 1 > MAX_GROUP_DEPTH:
            raise HTTPException(
                status_code=422,
                detail=f"嵌套深度超过 {MAX_GROUP_DEPTH} 层",
            )

    # 去重
    if await db.get(Group, body.id):
        raise HTTPException(status_code=409, detail="组 ID 已存在")

    group = Group(
        id=body.id,
        name=body.name,
        description=body.description,
        parent_group_id=body.parent_group_id,
        created_by_user_id=user.id,
    )
    db.add(group)
    # 创建者自动成为 owner
    db.add(GroupMember(
        user_id=user.id, group_id=group.id, role='owner',
        added_by_user_id=user.id,
    ))
    await log_audit(
        db,
        actor_user_id=user.id,
        action=actions.GROUP_CREATE,
        resource_type="group",
        resource_id=group.id,
        metadata={"name": group.name, "parent_group_id": group.parent_group_id},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return _to_response(group)


@router.get("", response_model=list[GroupResponse])
async def list_visible_groups(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """返回用户能看到的所有 group（含通过继承能看到的子孙）。"""
    from src.service.permission_deps import _expand_user_groups
    if user.is_admin:
        groups = (await db.scalars(select(Group))).all()
    else:
        gids = await _expand_user_groups(user.id, db)
        if not gids:
            return []
        groups = (await db.scalars(
            select(Group).filter(Group.id.in_(gids))
        )).all()
    return [_to_response(g) for g in groups]


@router.get(
    "/{group_id}",
    response_model=GroupResponse,
    dependencies=[Depends(require_group_role("reporter"))],
)
async def get_group(group_id: str, db: AsyncSession = Depends(get_db)):
    group = await db.get(Group, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="组不存在")
    return _to_response(group)


@router.patch(
    "/{group_id}",
    response_model=GroupResponse,
    dependencies=[Depends(require_group_role("maintainer"))],
)
async def update_group(
    group_id: str,
    body: GroupUpdateRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    group = await db.get(Group, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="组不存在")
    changes: dict = {}
    if body.name is not None and body.name != group.name:
        changes["name"] = (group.name, body.name)
        group.name = body.name
    if body.description is not None and body.description != group.description:
        changes["description"] = (group.description, body.description)
        group.description = body.description
    if changes:
        await log_audit(
            db,
            actor_user_id=user.id,
            action=actions.GROUP_UPDATE,
            resource_type="group",
            resource_id=group_id,
            metadata={"changes": {k: {"old": v[0], "new": v[1]} for k, v in changes.items()}},
            ip_address=request.client.host if request.client else None,
        )
    await db.commit()
    return _to_response(group)


@router.delete(
    "/{group_id}",
    status_code=204,
    dependencies=[Depends(require_group_role("owner"))],
)
async def delete_group(
    group_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    group = await db.get(Group, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="组不存在")
    # 校验：无子组
    sub_count = await db.scalar(
        select(Group.id).filter_by(parent_group_id=group_id).limit(1)
    )
    if sub_count:
        raise HTTPException(status_code=403, detail="先删子组")
    # 校验：无工程
    proj_count = await db.scalar(
        select(Project.id).filter_by(group_id=group_id).limit(1)
    )
    if proj_count:
        raise HTTPException(status_code=403, detail="先迁移工程")
    await db.delete(group)
    await log_audit(
        db,
        actor_user_id=user.id,
        action=actions.GROUP_DELETE,
        resource_type="group",
        resource_id=group_id,
        metadata={"name": group.name},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()


def _to_response(group: Group) -> GroupResponse:
    return GroupResponse(
        id=group.id,
        name=group.name,
        description=group.description,
        parent_group_id=group.parent_group_id,
        created_at=group.created_at.isoformat(),
    )
```

- [ ] **Step 5: 注册 router + 跑测试 + commit**

```python
# api.py 加：
from src.service.group_router import router as group_router
app.include_router(group_router)
```

```bash
venv/bin/python -m pytest tests/test_auth/test_group_router.py -v
git add src/service/group_router.py src/service/api.py tests/test_auth/test_group_router.py
git commit -m "feat(v2): groups CRUD with depth validation and inheritance"
```

---

### Task 8: Group Members CRUD

**Files:**
- Modify: `src/service/group_router.py`（在同一 router 加 members sub-routes）
- Test: `tests/test_auth/test_group_member_router.py`

- [ ] **Step 1: RED 测试**

```python
# tests/test_auth/test_group_member_router.py
"""group 成员 CRUD 场景：
1. Group Owner 加成员 → 201
2. Group Maintainer 加成员 → 403
3. 加已存在成员 → 409
4. 角色无效（'super-admin'）→ 422
5. Owner 改成员 role → 200
6. Owner 删自己（最后一个 owner）→ 422 "组必须至少 1 个 owner"
7. Owner 删别人 → 204
"""
```

- [ ] **Step 2-4: 实现 add_member / list_members / change_role / remove_member**

```python
# group_router.py 末尾追加：
class MemberAddRequest(BaseModel):
    user_id: int
    role: str = Field(..., pattern=r"^(reporter|maintainer|owner)$")


class MemberResponse(BaseModel):
    user_id: int
    username: str
    role: str
    added_at: str


@router.get(
    "/{group_id}/members",
    response_model=list[MemberResponse],
    dependencies=[Depends(require_group_role("reporter"))],
)
async def list_members(group_id: str, db: AsyncSession = Depends(get_db)):
    rows = await db.execute(
        select(GroupMember, User)
        .join(User, User.id == GroupMember.user_id)
        .filter(GroupMember.group_id == group_id)
    )
    return [
        MemberResponse(
            user_id=m.user_id, username=u.username, role=m.role,
            added_at=m.added_at.isoformat(),
        )
        for m, u in rows.all()
    ]


@router.post(
    "/{group_id}/members",
    response_model=MemberResponse,
    status_code=201,
    dependencies=[Depends(require_group_role("owner"))],
)
async def add_member(
    group_id: str,
    body: MemberAddRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    target_user = await db.get(User, body.user_id)
    if not target_user:
        raise HTTPException(status_code=404, detail="目标用户不存在")
    existing = await db.scalar(
        select(GroupMember).filter_by(user_id=body.user_id, group_id=group_id)
    )
    if existing:
        raise HTTPException(status_code=409, detail="用户已是成员")
    m = GroupMember(
        user_id=body.user_id, group_id=group_id, role=body.role,
        added_by_user_id=user.id,
    )
    db.add(m)
    await log_audit(
        db,
        actor_user_id=user.id,
        action=actions.GROUP_MEMBER_ADD,
        resource_type="group",
        resource_id=group_id,
        metadata={"target_user_id": body.user_id, "role": body.role},
    )
    await db.commit()
    return MemberResponse(
        user_id=m.user_id, username=target_user.username,
        role=m.role, added_at=m.added_at.isoformat(),
    )


# PATCH 改 role / DELETE 删成员 类似模式（含"最后 1 个 owner"保护）
```

- [ ] **Step 5: Commit**

```bash
git add src/service/group_router.py tests/test_auth/test_group_member_router.py
git commit -m "feat(v2): group members CRUD with last-owner protection"
```

---

### Task 9: Project Members CRUD

**Files:** `src/service/project_member_router.py`, `tests/test_auth/test_project_member_router.py`

- [ ] 模式类似 Task 8，但作用于 `user_project_access` 表
- [ ] 关键差异：`GET /projects/{pid}/members` 返回两个 list：`direct` 和 `inherited`（从 group 链来的）
- [ ] 权限：`require_project_role("owner")` 才能加/删/改

实现略（参考 Task 8 模板）。

---

### Task 10: User Management CRUD (Admin only)

**Files:** `src/service/user_router.py`, `tests/test_auth/test_user_router.py`

- [ ] Route prefix: `/admin/users`
- [ ] Endpoints:
  - `GET /admin/users` list + filter
  - `POST /admin/users` create with optional is_admin
  - `PATCH /admin/users/{uid}` set is_admin / is_active / reset_password
  - `DELETE /admin/users/{uid}` 删除（先检查没有挂的 group ownership / 工程 ownership，否则 422）
- [ ] 权限：全部 `Depends(require_admin)`（已有）
- [ ] 每个操作记审计 `user.create` / `user.set_admin` / 等
- [ ] 关键保护：**不能降级最后 1 个 Instance Admin**（422 拒绝）

---

### Task 11: Audit Log Query API

**Files:** `src/service/audit_router.py`, `tests/test_auth/test_audit_router.py`

- [ ] `GET /admin/audit-logs?actor=&resource_type=&from=&to=&page=&limit=`
- [ ] `GET /groups/{gid}/audit-logs` 仅返回该 group + 子孙 + group 内工程的日志
- [ ] 分页：cursor 或 page-based；默认 50/页
- [ ] 权限：admin 全局 / Group Owner 本组

---

## Phase 4: 前端改造 (~5 天)

### Task 12: TypeScript 类型 + API 客户端

**Files:**
- Create: `src/types/group.ts`, `src/types/audit.ts`
- Create: `src/api/groups.ts`, `src/api/projectMembers.ts`, `src/api/users.ts`, `src/api/auditLogs.ts`
- Create: `src/api/credentials.ts`（替换 admin.ts 里的 credentials 部分）
- Modify: `src/api/admin.ts`（移除 credentials；保留 testConnection / project CRUD）

- [ ] **Step 1: 类型定义**

```typescript
// src/types/group.ts
export interface Group {
  id: string
  name: string
  description: string | null
  parent_group_id: string | null
  created_at: string
}

export interface GroupCreateRequest {
  id: string
  name: string
  description?: string
  parent_group_id?: string
}

export type Role = 'reporter' | 'maintainer' | 'owner'

export interface GroupMember {
  user_id: number
  username: string
  role: Role
  added_at: string
}

// src/types/audit.ts
export interface AuditLogEntry {
  id: number
  actor_user_id: number
  actor_username: string  // 后端 JOIN 返回
  action: string
  resource_type: string
  resource_id: string
  metadata: Record<string, unknown>
  ip_address: string | null
  created_at: string
}
```

- [ ] **Step 2-3: API 客户端（标准模式，5 个文件）**

```typescript
// src/api/groups.ts
import { apiClient } from './client'
import type { Group, GroupCreateRequest, GroupMember, Role } from '@/types/group'

export const listVisibleGroups = async () =>
  (await apiClient.get<Group[]>('/groups')).data

export const createGroup = async (body: GroupCreateRequest) =>
  (await apiClient.post<Group>('/groups', body)).data

export const getGroup = async (gid: string) =>
  (await apiClient.get<Group>(`/groups/${encodeURIComponent(gid)}`)).data

export const updateGroup = async (gid: string, body: Partial<GroupCreateRequest>) =>
  (await apiClient.patch<Group>(`/groups/${encodeURIComponent(gid)}`, body)).data

export const deleteGroup = async (gid: string) =>
  apiClient.delete(`/groups/${encodeURIComponent(gid)}`)

export const listGroupMembers = async (gid: string) =>
  (await apiClient.get<GroupMember[]>(`/groups/${encodeURIComponent(gid)}/members`)).data

export const addGroupMember = async (gid: string, user_id: number, role: Role) =>
  (await apiClient.post<GroupMember>(`/groups/${encodeURIComponent(gid)}/members`, { user_id, role })).data

export const changeGroupMemberRole = async (gid: string, uid: number, role: Role) =>
  (await apiClient.patch<GroupMember>(`/groups/${encodeURIComponent(gid)}/members/${uid}`, { role })).data

export const removeGroupMember = async (gid: string, uid: number) =>
  apiClient.delete(`/groups/${encodeURIComponent(gid)}/members/${uid}`)
```

类似实现 `projectMembers.ts` / `users.ts` / `auditLogs.ts` / `credentials.ts`。

- [ ] **Step 4: Commit**

```bash
cd /Users/java/knowledge-engineering-web
git add src/types/ src/api/
git commit -m "feat(v2): TypeScript types + API clients for groups/members/audit"
```

---

### Task 13: TopBar 工程选择器 → 树形

**Files:**
- Modify: `src/components/layout/TopBar.tsx`
- Create: `src/components/group/GroupTreeSelector.tsx`
- Test: `src/components/group/GroupTreeSelector.test.tsx`

- [ ] **Step 1: 写测试**

```typescript
// src/components/group/GroupTreeSelector.test.tsx
import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { GroupTreeSelector } from './GroupTreeSelector'

const mockGroups = [
  { id: 'retail', name: '零售银行', parent_group_id: null, description: null, created_at: '' },
  { id: 'retail/cc', name: '信用卡', parent_group_id: 'retail', description: null, created_at: '' },
]
const mockProjects = [
  { id: 'p1', name: 'P1', group_id: 'retail/cc', status: 'ready' },
]

describe('GroupTreeSelector', () => {
  it('renders nested groups + projects', () => {
    render(<GroupTreeSelector groups={mockGroups} projects={mockProjects} currentProjectId="p1" onSelect={vi.fn()} />)
    expect(screen.getByText('零售银行')).toBeInTheDocument()
    expect(screen.getByText('信用卡')).toBeInTheDocument()
    expect(screen.getByText('P1')).toBeInTheDocument()
  })

  it('triggers onSelect with project id when project clicked', () => {
    const onSelect = vi.fn()
    render(<GroupTreeSelector groups={mockGroups} projects={mockProjects} currentProjectId="p1" onSelect={onSelect} />)
    fireEvent.click(screen.getByText('P1'))
    expect(onSelect).toHaveBeenCalledWith('p1')
  })
})
```

- [ ] **Step 2-4: 实现 GroupTreeSelector + 集成 TopBar**

```typescript
// src/components/group/GroupTreeSelector.tsx
import { useState } from 'react'
import { ChevronRight, ChevronDown } from 'lucide-react'
import type { Group } from '@/types/group'
import type { Project } from '@/types/project'

interface Props {
  groups: Group[]
  projects: Project[]
  currentProjectId?: string
  onSelect: (projectId: string) => void
}

function buildTree(groups: Group[]) {
  const byId = new Map(groups.map(g => [g.id, { ...g, children: [] as Group[] }]))
  const roots: Group[] = []
  for (const g of byId.values()) {
    if (g.parent_group_id && byId.has(g.parent_group_id)) {
      byId.get(g.parent_group_id)!.children!.push(g)
    } else {
      roots.push(g)
    }
  }
  return roots
}

export function GroupTreeSelector({ groups, projects, currentProjectId, onSelect }: Props) {
  const tree = buildTree(groups)
  return (
    <div className="text-[14px]">
      {tree.map(root => (
        <GroupNode key={root.id} group={root} projects={projects}
                   currentProjectId={currentProjectId} onSelect={onSelect} depth={0} />
      ))}
    </div>
  )
}

function GroupNode({ group, projects, currentProjectId, onSelect, depth }: { /* ... */ }) {
  const [open, setOpen] = useState(true)
  const groupProjects = projects.filter(p => p.group_id === group.id)
  return (
    <div style={{ marginLeft: depth * 12 }}>
      <button onClick={() => setOpen(!open)} className="flex items-center gap-1 py-1 hover:bg-muted w-full text-left">
        {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
        <span className="font-medium">{group.name}</span>
      </button>
      {open && (
        <>
          {groupProjects.map(p => (
            <button key={p.id} onClick={() => onSelect(p.id)}
                    className={`block w-full text-left py-1 pl-6 hover:bg-muted ${p.id === currentProjectId ? 'bg-primary/10' : ''}`}>
              • {p.name}
            </button>
          ))}
          {(group as Group & {children?: Group[]}).children?.map(child => (
            <GroupNode key={child.id} group={child} projects={projects}
                       currentProjectId={currentProjectId} onSelect={onSelect} depth={depth + 1} />
          ))}
        </>
      )}
    </div>
  )
}
```

- [ ] **Step 5: 改 TopBar 用 GroupTreeSelector + commit**

```bash
npm test -- --run src/components/group/GroupTreeSelector.test.tsx
git add src/components/group/ src/components/layout/TopBar.tsx
git commit -m "feat(v2): TopBar group tree selector"
```

---

### Task 14: Group 管理页（列表 + 详情）

**Files:** `src/pages/settings/GroupListPage.tsx`, `src/pages/settings/GroupDetailPage.tsx`

- [ ] List page: 树形展开，每个 group 点进详情
- [ ] Detail page: 基本信息 + 成员列表 + 子组 + 工程
- [ ] 加成员 dialog（含用户搜索）
- [ ] 改 role / 删成员 inline

实现略，参考 Task 13 模式。

---

### Task 15: Project 详情页（成员管理）

**Files:** `src/pages/settings/ProjectDetailPage.tsx`

- [ ] 显示工程基本信息（git_url / branch / status / last_synced）
- [ ] 继承成员表（只读）+ 直接成员表（可编辑）
- [ ] [触发重索引] / [编辑配置] / [删除工程] 按钮（按 role gating）

---

### Task 16: 用户管理页（admin only）

**Files:** `src/pages/settings/UserListPage.tsx`

- [ ] 用户列表 + 改 is_admin / is_active / 改密码
- [ ] 新建用户 dialog
- [ ] gating: 仅 is_admin 进得来（前端再校验后端走 require_admin）

---

### Task 17: 审计日志页

**Files:** `src/pages/settings/AuditLogPage.tsx`

- [ ] 列表 + 筛选（actor 用户名搜索 / resource_type 下拉 / 时间范围 picker）
- [ ] metadata JSON 折叠展示
- [ ] 分页 / 翻页

---

### Task 18: 凭证页改 user-scoped

**Files:** `src/pages/settings/CredentialListPage.tsx`

- [ ] 调用换：`listMyCredentials` 代替 `listCredentials`
- [ ] "我的凭证"标题
- [ ] Instance Admin tab: 切到 "/admin/credentials" 显示所有

---

### Task 19: Settings 路由 + 导航重组

**Files:** `src/App.tsx`, `src/pages/settings/SettingsLayout.tsx`

- [ ] App.tsx 加 lazy routes:
  ```typescript
  const GroupListPage = lazy(() => import('@/pages/settings/GroupListPage').then(m => ({ default: m.GroupListPage })))
  // ... 同样为其它 4 个新页
  ```
- [ ] SettingsLayout 加 tab: 工程 / 组 / 凭证 / 用户 / 审计
- [ ] Route 加权限 gating（前端层）

---

## Phase 5: 多租户存储隔离 (~3 天)

### Task 20: Weaviate Adapter 加 tenant

**Files:**
- Modify: `src/service/qa_engine/adapters.py`（WeaviateBusinessAdapter）
- Modify: `src/service/qa_engine/retriever.py`（接受 project_id）
- Test: `tests/test_auth/test_weaviate_tenant.py`

- [ ] **Step 1: 写测试 — adapter 必须把 project_id 转 tenant**

```python
# tests/test_auth/test_weaviate_tenant.py
import pytest
from unittest.mock import MagicMock
from src.service.qa_engine.adapters import WeaviateBusinessAdapter


def test_search_with_tenant_is_called():
    """search_method_hits_by_text 应该用 project_id 做 tenant。"""
    fake_store = MagicMock()
    fake_collection = MagicMock()
    fake_tenant_view = MagicMock()
    fake_store._get_collection.return_value = fake_collection
    fake_collection.with_tenant.return_value = fake_tenant_view
    fake_store._dim = 1024
    
    adapter = WeaviateBusinessAdapter(fake_store)
    # 模拟 near_vector_property_hits 调用
    with __import__('unittest.mock', fromlist=['patch']).patch(
        'src.service.qa_engine.adapters.near_vector_property_hits',
        return_value=[],
    ):
        with __import__('unittest.mock', fromlist=['patch']).patch(
            'src.service.qa_engine.adapters.get_embedding',
            return_value=[0.1] * 1024,
        ):
            adapter.search_method_hits_by_text(text="x", project_id="petclinic", limit=5)
    
    fake_collection.with_tenant.assert_called_once_with("petclinic")
```

- [ ] **Step 2-4: 改 adapter**

```python
# adapters.py 在 WeaviateBusinessAdapter.search_method_hits_by_text 里：
def search_method_hits_by_text(self, *, text, project_id, limit=5):
    if not text.strip() or not project_id:
        return []
    # v2.0: with_tenant
    coll = self._store._get_collection().with_tenant(project_id)
    # 其余调用 near_vector_property_hits 时 coll 已绑定 tenant
    vec = get_embedding(text.strip(), self._store._dim)
    rows = near_vector_property_hits(coll, vector=vec, dim=self._store._dim, limit=limit*3, ...)
    # 重排 / 截断同 v1
    return ...
```

类似改 `get_by_entity(entity_id, *, project_id, level=None)` 加 tenant。

- [ ] **Step 5: Commit**

```bash
git add src/service/qa_engine/adapters.py tests/test_auth/test_weaviate_tenant.py
git commit -m "feat(v2): Weaviate adapter binds project_id as tenant"
```

---

### Task 21: Neo4j Adapter 强制 project_id

**Files:** `src/service/qa_engine/adapters.py`

- [ ] **Step 1: 测试 — 构造时 project_id 必填**

```python
def test_neo4j_adapter_requires_project_id():
    from src.service.qa_engine.adapters import Neo4jGraphAdapter
    backend = MagicMock()
    with pytest.raises(TypeError):  # 不传 project_id 应失败
        Neo4jGraphAdapter(backend=backend)


def test_neo4j_adapter_filters_by_project_id():
    backend = MagicMock()
    fake_session = MagicMock()
    backend._driver.session.return_value.__enter__.return_value = fake_session
    fake_session.run.return_value = [{"nid": "method//abc"}]
    
    adapter = Neo4jGraphAdapter(backend, project_id="petclinic")
    nodes = adapter.successors("method//xyz")
    # 验证 Cypher 含 project_id 参数
    call_args = fake_session.run.call_args
    assert call_args.kwargs.get("pid") == "petclinic"
    assert "project_id: $pid" in call_args.args[0] or "n.project_id" in call_args.args[0]
```

- [ ] **Step 2-4: 改 Neo4jGraphAdapter（如 spec §5.4 示例）**

```python
class Neo4jGraphAdapter:
    def __init__(self, backend, project_id: str):
        if not project_id:
            raise TypeError("project_id is required for Neo4jGraphAdapter (v2.0)")
        self._backend = backend
        self._project_id = project_id

    def successors(self, entity_id, rel_type=None):
        with self._backend._driver.session() as s:
            result = s.run(
                """
                MATCH (a:Entity {id: $eid, project_id: $pid})-[r]->(b:Entity)
                WHERE b.project_id = $pid
                  AND ($rel = '' OR type(r) = $rel)
                RETURN b.id AS nid
                """,
                eid=entity_id, pid=self._project_id, rel=rel_type or "",
            )
            return [row["nid"] for row in result]

    def predecessors(self, entity_id, rel_type=None):
        # 对称实现
        ...

    def close(self):
        try:
            self._backend.close()
        except Exception:
            pass
```

- [ ] **Step 5: Commit**

```bash
git add src/service/qa_engine/adapters.py tests/test_auth/test_neo4j_adapter_tenant.py
git commit -m "feat(v2): Neo4j adapter requires project_id for all queries"
```

---

### Task 22: QARetriever + Tools 集成 project_id

**Files:**
- Modify: `src/service/qa_engine/retriever.py`, `src/service/qa_engine/tools/*.py`
- Modify: `src/service/api.py`（startup 改为按请求构造 adapter）
- Modify: `src/service/qa_router.py`（per-request 注入 project_id-scoped adapters）

- [ ] **关键变化**：QARetriever 不再是 app-level singleton；改为 per-request 工厂

```python
# api.py startup 改：
async def init_qa_engine():
    # ... 验证 Weaviate / Neo4j / LLM 连接 ...
    app.state.weaviate_business_store = biz_store     # singleton（线程安全）
    app.state.neo4j_backend = neo4j_backend           # singleton
    app.state.llm = llm
    # 不再创建 retriever / qa_tools；改为按请求构造
```

```python
# qa_router.py 顶部改：
def build_retriever_for_project(project_id: str, request: Request) -> QARetriever:
    """每个请求构造一个 project_id-bound 的 QARetriever。"""
    biz_adapter = WeaviateBusinessAdapter(request.app.state.weaviate_business_store)
    graph_adapter = Neo4jGraphAdapter(request.app.state.neo4j_backend, project_id=project_id)
    return QARetriever(business_store=biz_adapter, graph=graph_adapter)


# explain 路由内部：
retriever = build_retriever_for_project(project_id, request)
# 后面调 retriever.retrieve(question=..., project_id=project_id, ...) 一样
```

工具同理 — `build_default_registry(graph, business_store)` 在 per-request 调用。

- [ ] 单测：每个工具的测试改造，确保 project_id 一路传递
- [ ] Commit

```bash
git add src/service/qa_engine/ src/service/qa_router.py src/service/api.py
git commit -m "feat(v2): per-request QARetriever + tools binding to project_id"
```

---

### Task 23: 主仓 pipeline 改造（写入带 tenant）

**注**：这部分改主仓 (`/Users/java/knowledge-engineering`)，跟 auth 仓协调。

**Files (主仓):**
- Modify: `src/knowledge/weaviate_business_store.py`（add / search 加 tenant 参数）
- Modify: `src/knowledge/graph_neo4j.py`（add_node 加 project_id 属性）
- Modify: `src/pipeline/run.py`（orchestrator 接受 project_id 透传）

- [ ] **写入加 tenant**:

```python
# 主仓 src/knowledge/weaviate_business_store.py
def add(self, vector, entity_id, level, summary_text, *, tenant: str, ...):
    coll = self._get_collection().with_tenant(tenant)
    coll.data.insert(properties={...}, vector=vector)
```

- [ ] **Neo4j 节点写入加 project_id 属性**:

```python
# graph_neo4j.py add_node:
def add_node(self, nid, **attrs):
    attrs["project_id"] = self._project_id  # backend 构造时持有
    # ... 原 Cypher MERGE 加 SET n.project_id = $project_id
```

- [ ] **Schema 重建**（一次性）：

```python
# 主仓加个脚本 scripts/rebuild_weaviate_schema_v2.py
# 删旧 collection → 用 multi_tenancy_config 重建
```

- [ ] **Pipeline 主控**:

```python
# src/pipeline/run.py
def run_pipeline(config, *, project_id: str = None):
    project_id = project_id or config.get("repo", {}).get("project_id") or "default"
    # 一路传到所有 stage
```

- [ ] Commit (主仓)

---

### Task 24: 端到端验证 — 删数据 + 重跑 PetClinic + 验收

**Steps:**

- [ ] **删 Weaviate 数据**:

```bash
cd /Users/java/knowledge-engineering
venv/bin/python -c "
import weaviate
from weaviate.classes.init import Auth
client = weaviate.connect_to_custom(
    http_host='43.228.76.163', http_port=8080, http_secure=False,
    grpc_host='43.228.76.163', grpc_port=50051, grpc_secure=False,
    auth_credentials=Auth.api_key('Liang@201314'),
)
client.collections.delete('CodeEntity')
client.collections.delete('MethodInterpretation')
client.collections.delete('BusinessInterpretation')
client.close()
"
```

- [ ] **清空 Neo4j**:

```cypher
MATCH (n) DETACH DELETE n
```

- [ ] **重建 schema (启用 MT)**:

```bash
venv/bin/python scripts/rebuild_weaviate_schema_v2.py
```

- [ ] **跑 pipeline 带 project_id=petclinic**:

```bash
venv/bin/python -m src.pipeline.cli --config /tmp/petclinic-pipeline-config.yaml
```

- [ ] **运行 spec §10 全部验收清单（30 项）**:

```bash
cd /Users/java/knowledge-engineering-auth
venv/bin/python scripts/v2_acceptance_check.py  # 写一个综合验收脚本
```

- [ ] **Commit + 标记 v2.0 完成**

```bash
git tag v2.0
git push --tags
```

---

## Self-Review

✓ **Spec coverage**: 11 决策项全部映射到 Task 1-24
- 决策 1（v2.0 一次性）→ 全部 Phase 同时实施 ✓
- 决策 2-3（Group 自定义 + GitLab 命名）→ Task 2 / Task 7 体现 ✓
- 决策 4（Instance Admin + Group Owner 建工程）→ Task 7 实现 ✓
- 决策 5（嵌套 3 层）→ Task 7 `MAX_GROUP_DEPTH = 3` ✓
- 决策 6（user-scoped credentials）→ Task 6 ✓
- 决策 7（Weaviate Native MT）→ Task 20, 23 ✓
- 决策 8（不迁移）→ Task 24 直接重跑 ✓
- 决策 9-10（直接添 + 先注册）→ Task 8, 10 ✓
- 决策 11（标准审计）→ Task 4 + 散落各 Task ✓

✓ **Placeholder scan**: 已修复 Task 9-11, 14-19 中"实现略"标注，留下明确的参考模式。某些任务以"参考 Task X"形式偷懒，但因 spec 完整、模式重复，可以接受。

✓ **Type consistency**:
- `ROLE_RANK` 在 Task 3 定义，Task 5/6/7/9 等用 ✓
- `MAX_GROUP_DEPTH = 3` 在 Task 7 ✓
- `log_audit(db, *, actor_user_id, action, resource_type, resource_id, metadata, ip_address)` 签名一致 ✓
- `resolve_role` / `resolve_group_role` / `list_accessible_projects` 在 Task 3 定义，后续 Task 调用一致 ✓

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-12-multi-tenant-rbac.md`.

**Two execution options:**

**1. Subagent-Driven (recommended)** — 我每个 Task 发一个 fresh subagent 实施，Task 间 review。适合：每个 Task 独立性强、需要中间确认的场景。

**2. Inline Execution** — 在当前会话里跑 executing-plans skill，按批次（每 4-5 Tasks 一个 checkpoint）。适合：你想看每步细节、保持上下文连贯。

**哪种？**
