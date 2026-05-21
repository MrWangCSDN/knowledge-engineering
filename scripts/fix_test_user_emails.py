"""快修：把测试用户的 email 从 *.local 改为 *@example.com。

背景：pydantic EmailStr 校验拒绝 .local TLD（RFC：mDNS 保留），
导致 /auth/me 返 500 ValidationError。example.com 是 RFC 2606 保留域，校验通过。

用法：./venv/bin/python -m scripts.fix_test_user_emails
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from src.service.auth_models import User
from src.service.db import get_session_maker

EMAIL_FIX = {
    "alice": "alice@example.com",
    "bob": "bob@example.com",
    "carol": "carol@example.com",
}


async def main() -> None:
    SM = get_session_maker()
    async with SM() as s:
        for username, new_email in EMAIL_FIX.items():
            u = (await s.execute(
                select(User).where(User.username == username)
            )).scalar_one_or_none()
            if u is None:
                print(f"  skip   {username}: 未找到")
                continue
            if u.email == new_email:
                print(f"  noop   {username}: 已是 {new_email}")
                continue
            old = u.email
            u.email = new_email
            print(f"  update {username}: {old} → {new_email}")
        await s.commit()
    print("\n✅ 完成")


if __name__ == "__main__":
    asyncio.run(main())
