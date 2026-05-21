"""为记忆隔离测试创建 3 用户 + 3 工程 + 访问矩阵（idempotent，可重复跑）。

用法：./venv/bin/python -m scripts.setup_test_users

矩阵：
  alice → proj-a (owner), proj-b (reporter)
  bob   → proj-b (owner), proj-c (reporter)
  carol → proj-c (owner), proj-a (reporter)

密码统一为 "test12345"（8 字符以上，符合 auth_security 校验）。

测试场景覆盖：
- 用户级记忆跨用户隔离（alice/bob/carol 各自 ke://u/{uid}/global/...）
- 工程级记忆跨工程分支（同 user 在 proj-a vs proj-b 应分目录）
- 跨工程访问（alice 在 proj-a 是 owner，在 proj-b 是 reporter）
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from src.service import auth_security as sec
from src.service.auth_models import User
from src.service.db import get_session_maker
from src.service.db_models_homepage import Project, UserProjectAccess

USERS = [
    # (username, email, password)
    # 邮箱用 example.com（RFC 2606 保留域，pydantic email 校验通过；
    # 原 .local 是 mDNS 保留 TLD，pydantic EmailStr 会拒，导致 /auth/me ValidationError）
    ("alice", "alice@example.com", "test12345"),
    ("bob", "bob@example.com", "test12345"),
    ("carol", "carol@example.com", "test12345"),
]

PROJECTS = [
    # (project_id, name)
    ("proj-a", "测试工程 A（alice 主）"),
    ("proj-b", "测试工程 B（bob 主）"),
    ("proj-c", "测试工程 C（carol 主）"),
]

ACCESS = [
    # (username, project_id, role)
    ("alice", "proj-a", "owner"),
    ("alice", "proj-b", "reporter"),
    ("bob", "proj-b", "owner"),
    ("bob", "proj-c", "reporter"),
    ("carol", "proj-c", "owner"),
    ("carol", "proj-a", "reporter"),
]


async def main() -> None:
    """创建 / 同步测试数据；幂等可重跑。"""
    SM = get_session_maker()
    async with SM() as s:
        # ── Users ─────────────────────────────────────────────────────────
        user_id_map: dict[str, int] = {}
        for username, email, password in USERS:
            existing = (await s.execute(
                select(User).where(User.username == username)
            )).scalar_one_or_none()
            if existing is not None:
                user_id_map[username] = existing.id
                print(f"  user[skip] {username:6s} id={existing.id} email={existing.email}")
                continue
            u = User(
                email=email,
                username=username,
                hashed_password=sec.hash_password(password),
                is_active=True,
                is_admin=False,
            )
            s.add(u)
            await s.flush()
            user_id_map[username] = u.id
            print(f"  user[new ] {username:6s} id={u.id} email={u.email}")

        # ── Projects ──────────────────────────────────────────────────────
        for pid, name in PROJECTS:
            existing = (await s.execute(
                select(Project).where(Project.id == pid)
            )).scalar_one_or_none()
            if existing is not None:
                print(f"  proj[skip] {pid:8s} name={existing.name}")
                continue
            p = Project(
                id=pid,
                name=name,
                language="java",
                status="ready",  # 直接标 ready 让 qa endpoint 可用
            )
            s.add(p)
            print(f"  proj[new ] {pid:8s} name={name}")
        await s.flush()

        # ── UserProjectAccess ────────────────────────────────────────────
        for username, pid, role in ACCESS:
            uid = user_id_map[username]
            existing = (await s.execute(
                select(UserProjectAccess).where(
                    UserProjectAccess.user_id == uid,
                    UserProjectAccess.project_id == pid,
                )
            )).scalar_one_or_none()
            if existing is not None:
                print(f"  acc [skip] {username:6s}@{pid:8s} role={existing.role}")
                continue
            a = UserProjectAccess(user_id=uid, project_id=pid, role=role)
            s.add(a)
            print(f"  acc [new ] {username:6s}@{pid:8s} role={role}")

        await s.commit()

    # ── 打印最终状态（新连接读最新数据）──────────────────────────────────
    print("\n=== 最终状态 ===")
    async with SM() as s:
        print("\n[Users]")
        for u in (await s.execute(select(User).order_by(User.id))).scalars().all():
            badge = "ADMIN" if u.is_admin else "user "
            print(f"  id={u.id:>3}  {badge}  {u.username:6s}  {u.email}")

        print("\n[Projects]")
        for p in (await s.execute(select(Project).order_by(Project.id))).scalars().all():
            print(f"  {p.id:8s}  status={p.status:8s}  name={p.name}")

        print("\n[UserProjectAccess 矩阵]")
        rows = (await s.execute(
            select(UserProjectAccess, User.username)
            .join(User, UserProjectAccess.user_id == User.id)
            .order_by(User.username, UserProjectAccess.project_id)
        )).all()
        for row in rows:
            access, uname = row
            print(f"  {uname:6s} @ {access.project_id:8s}  role={access.role}")

    print("\n=== 登录凭据 ===")
    for username, email, _ in USERS:
        print(f"  {username:6s}  password=test12345  (email: {email})")


if __name__ == "__main__":
    asyncio.run(main())
