"""离线 re-encrypt：把所有加密 token 迁到当前主 key（MultiFernet.rotate）。
入口 python -m src.service.token_reencrypt。轮换流程见 P5c spec §4.3。
注意：对已部署生产库运行（KE_DB_URL 指向生产）；本 CLI 不建表/不跑迁移。"""
from __future__ import annotations

import logging

from sqlalchemy import select

from src.service.db_models_homepage import GitCredential, UserScmToken
from src.service.token_crypto import rotate_token

_log = logging.getLogger(__name__)


async def reencrypt_all_tokens(session) -> dict:
    """遍历 git_credentials + user_scm_token，逐条 rotate 到主 key。
    解不开的行 log+skip+计 error，不崩不丢行。返回计数 dict。"""
    counts = {"git_credentials": 0, "user_scm_token": 0, "errors": 0}
    for cred in (await session.execute(select(GitCredential))).scalars().all():
        try:
            cred.encrypted_token = rotate_token(cred.encrypted_token)
            counts["git_credentials"] += 1
        except ValueError:
            counts["errors"] += 1
            _log.warning("reencrypt skip git_credential id=%s（密文解不开）", cred.id)
    for tok in (await session.execute(select(UserScmToken))).scalars().all():
        try:
            tok.access_token = rotate_token(tok.access_token)
            if tok.refresh_token:
                tok.refresh_token = rotate_token(tok.refresh_token)
            counts["user_scm_token"] += 1
        except ValueError:
            counts["errors"] += 1
            _log.warning("reencrypt skip user_scm_token id=%s（密文解不开）", tok.id)
    await session.commit()
    return counts


def _main() -> None:  # pragma: no cover — 进程入口
    import asyncio
    import sys
    from src.service.db import get_session_maker
    maker = get_session_maker()
    async def _run() -> dict:
        async with maker() as s:
            return await reencrypt_all_tokens(s)
    counts = asyncio.run(_run())
    print(counts)
    # 安全闸：有解不开的行 → 非零退出，提醒运维**勿在 errors>0 时弃旧 key**（否则未迁行将永久无法解密）
    if counts["errors"]:
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    _main()
