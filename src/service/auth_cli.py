"""auth CLI：create-admin / list-users / reset-password。

用法：
  python -m src.service.auth_cli create-admin --email x@y.com --username admin --password 'xxx'
  python -m src.service.auth_cli list-users
  python -m src.service.auth_cli reset-password --username admin --password 'newpwd'
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from src.service import auth_security as sec
from src.service.auth_models import User
from src.service.db import get_session_maker


async def cmd_create_admin(email: str, username: str, password: str) -> int:
    if len(password) < 8:
        print("❌ password 至少 8 字符", file=sys.stderr); return 2
    SM = get_session_maker()
    async with SM() as s:
        existing = (await s.execute(
            select(User).where((User.email == email) | (User.username == username))
        )).scalar_one_or_none()
        if existing:
            print(f"❌ 已存在 user: id={existing.id} username={existing.username}", file=sys.stderr)
            return 3
        u = User(
            email=email, username=username,
            hashed_password=sec.hash_password(password),
            is_active=True, is_admin=True,
        )
        s.add(u)
        await s.commit()
        print(f"✅ 已创建 admin: id={u.id} email={u.email} username={u.username}")
    return 0


async def cmd_list_users() -> int:
    SM = get_session_maker()
    async with SM() as s:
        users = (await s.execute(select(User).order_by(User.id))).scalars().all()
        if not users:
            print("(no users)")
            return 0
        print(f"{'ID':>4}  {'USERNAME':20s}  {'EMAIL':30s}  ADMIN  ACTIVE  CREATED")
        for u in users:
            print(f"{u.id:>4}  {u.username:20s}  {u.email:30s}  "
                  f"{'✓' if u.is_admin else ' ':5}  {'✓' if u.is_active else ' ':6}  {u.created_at}")
    return 0


async def cmd_reset_password(username: str, password: str) -> int:
    if len(password) < 8:
        print("❌ password 至少 8 字符", file=sys.stderr); return 2
    SM = get_session_maker()
    async with SM() as s:
        user = (await s.execute(
            select(User).where(User.username == username)
        )).scalar_one_or_none()
        if user is None:
            print(f"❌ no such user: {username}", file=sys.stderr); return 3
        user.hashed_password = sec.hash_password(password)
        user.failed_attempts = 0
        user.locked_until = None
        await s.commit()
        print(f"✅ 已重置 user {user.username} 的密码 + 解除锁定")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="auth_cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("create-admin")
    pa.add_argument("--email", required=True)
    pa.add_argument("--username", required=True)
    pa.add_argument("--password", required=True)

    sub.add_parser("list-users")

    pr = sub.add_parser("reset-password")
    pr.add_argument("--username", required=True)
    pr.add_argument("--password", required=True)

    args = p.parse_args()
    if args.cmd == "create-admin":
        return asyncio.run(cmd_create_admin(args.email, args.username, args.password))
    if args.cmd == "list-users":
        return asyncio.run(cmd_list_users())
    if args.cmd == "reset-password":
        return asyncio.run(cmd_reset_password(args.username, args.password))
    return 1


if __name__ == "__main__":
    sys.exit(main())
