"""per-user SCM token 存取（Fernet）+ get_valid_scm_token 刷新（rotation-aware，fail-closed）。设计 §4.6/§5。"""
from __future__ import annotations

# uuid 用于生成主键；datetime/timezone 用于时间比较和 linked_at 首次写入
import uuid
from datetime import datetime, timezone
# Awaitable/Callable 是类型注解工具：表示"异步可调用"函数；Optional 表示可以为 None
from typing import Awaitable, Callable, Optional

# SQLAlchemy 的 delete/select 语句构造函数（ORM 风格）
from sqlalchemy import delete, select
# AsyncSession 是异步数据库会话类型，用于类型注解
from sqlalchemy.ext.asyncio import AsyncSession

# ORM 模型：每个用户对每个 provider 一行 token
from src.service.db_models_homepage import UserScmToken
# Fernet 加解密工具：encrypt_token 加密明文，decrypt_token 解密密文
from src.service.token_crypto import encrypt_token, decrypt_token

# 类型别名：refresh_fn 的签名是 async (refresh_token: str) -> dict
# dict 里至少有 access_token，可选 refresh_token / expires_at（rotation 时才有新 refresh_token）
RefreshFn = Callable[[str], Awaitable[dict]]


class ScmTokenInvalid(Exception):
    """token 失效且无法刷新（需用户重新关联）。

    由 get_valid_scm_token 在以下情况抛出：
    - 无 token 行（未关联）
    - 已过期 + 无 refresh_fn / 无 refresh_token
    - refresh_fn 自身抛 ScmTokenInvalid（如 invalid_grant）
    所有路径均先删 DB 行再抛出，保证 fail-closed。
    """


async def upsert_token(
    session: AsyncSession,
    *,                              # * 后全为关键字参数，避免位置参数顺序混淆
    user_id: int,
    provider: str,
    access_token: str,
    refresh_token: Optional[str],
    expires_at: Optional[datetime],
    scopes: Optional[str],
    scm_login: Optional[str],
) -> None:
    """按 (user_id, provider) upsert：access/refresh 加密落库；linked_at 仅首次设、复写不 bump。

    Args:
        session:       AsyncSession，调用方持有并负责 commit
        user_id:       users.id（整数 PK）
        provider:      如 'github' / 'gitlab'
        access_token:  明文 access token（将 Fernet 加密后写库）
        refresh_token: 明文 refresh token；None 表示 provider 不发新 refresh（保留旧的）
        expires_at:    过期时间（UTC）；None 表示永不过期
        scopes:        授权范围字符串，如 'read:user repo'
        scm_login:     SCM 平台用户名，每次 upsert 刷新（用于展示）
    """
    # 查询是否已有该 (user_id, provider) 的行
    row = (await session.execute(
        select(UserScmToken).where(
            UserScmToken.user_id == user_id,
            UserScmToken.provider == provider,
        )
    )).scalar_one_or_none()

    # 记录当前 UTC 时间，用于 linked_at（仅首次）和 updated_at（复写时刷新）
    now = datetime.now(timezone.utc)

    # 加密 access_token（不管是新建还是更新，都重新加密）
    enc_access = encrypt_token(access_token)
    # 仅当调用方传入了新 refresh_token 时才加密；None 表示"不更新 refresh_token"
    enc_refresh = encrypt_token(refresh_token) if refresh_token else None

    if row is None:
        # 首次关联：新建行，linked_at 设为 now（此后任何 upsert 都不再修改它）
        session.add(UserScmToken(
            id=f"ust-{uuid.uuid4().hex[:16]}",   # 前缀 + 16 位随机 hex 作为主键
            user_id=user_id,
            provider=provider,
            access_token=enc_access,
            refresh_token=enc_refresh,
            expires_at=expires_at,
            scopes=scopes,
            scm_login=scm_login,
            linked_at=now,                        # 首次关联时间，永久固定
        ))
    else:
        # 复写：只更新 token / 过期时间 / scopes / scm_login，linked_at 不动
        row.access_token = enc_access
        # rotation-aware：provider 不总是返回新 refresh_token；None 时保留旧加密值
        if enc_refresh is not None:
            row.refresh_token = enc_refresh
        row.expires_at = expires_at
        row.scopes = scopes
        row.scm_login = scm_login
        row.updated_at = now

    # flush 把改动写入本次事务（但尚未 commit），让同一 session 内的后续查询能看到
    await session.flush()


async def get_valid_scm_token(
    session: AsyncSession,
    *,
    user_id: int,
    provider: str,
    refresh_fn: Optional[RefreshFn],
) -> str:
    """取有效 access token：未过期→解密返回；过期→用 refresh_fn 刷新+rotation upsert；
    无 token / 无法刷新 → ScmTokenInvalid（已清除失效行）。

    Args:
        session:    AsyncSession
        user_id:    users.id
        provider:   如 'github'
        refresh_fn: async (plain_refresh_token: str) -> dict
                    dict 结构：{"access_token": str, "refresh_token"?: str, "expires_at"?: datetime}
                    若 provider 返回 invalid_grant，refresh_fn 应抛 ScmTokenInvalid

    Returns:
        明文 access_token（有效期内或刚刚刷新的）

    Raises:
        ScmTokenInvalid: token 不存在、已过期无法刷新、或刷新失败；调用前已删 DB 行
    """
    # 查询该用户的 provider token 行
    row = (await session.execute(
        select(UserScmToken).where(
            UserScmToken.user_id == user_id,
            UserScmToken.provider == provider,
        )
    )).scalar_one_or_none()

    # 没有 token 行 → 用户从未关联该 provider
    if row is None:
        raise ScmTokenInvalid(f"{provider} 未关联")

    # 处理时区：SQLite 存的 datetime 可能没有 tzinfo，统一补 UTC
    exp = row.expires_at
    if exp is not None and exp.tzinfo is None:
        # replace 不修改数值，只加上 UTC 时区标记
        exp = exp.replace(tzinfo=timezone.utc)

    # 未过期（expires_at 为 None 视为永不过期）：直接解密返回
    if exp is None or exp > datetime.now(timezone.utc):
        return decrypt_token(row.access_token)

    # ── 以下：token 已过期，尝试刷新 ──

    # 没有 refresh_fn 或 refresh_token → 无法刷新，删行并报错（fail-closed）
    if refresh_fn is None or not row.refresh_token:
        await session.delete(row)
        await session.flush()
        raise ScmTokenInvalid(f"{provider} token 已过期且无法刷新")

    try:
        # 解密旧 refresh_token（明文），传给 refresh_fn 换新 token
        new = await refresh_fn(decrypt_token(row.refresh_token))
    except ScmTokenInvalid:
        # refresh_fn 主动抛 ScmTokenInvalid（如 invalid_grant / revoked）
        # fail-closed：先删行，再把异常向上透传
        await session.delete(row)
        await session.flush()
        raise   # re-raise 同一个异常，保留原始 traceback

    # 刷新成功：rotation-aware upsert
    # new.get("refresh_token") 为 None 时 upsert_token 会保留旧加密 refresh_token
    await upsert_token(
        session,
        user_id=user_id,
        provider=provider,
        access_token=new["access_token"],
        refresh_token=new.get("refresh_token"),   # 可能为 None（非 rotation provider）
        expires_at=new.get("expires_at"),
        scopes=row.scopes,
        scm_login=row.scm_login,
    )
    # 返回新 access_token（明文，不经 DB 再读一次）
    return new["access_token"]


async def delete_token(
    session: AsyncSession,
    *,
    user_id: int,
    provider: str,
) -> None:
    """删除某 provider 的 token 行（解除关联用）。

    Args:
        session:  AsyncSession
        user_id:  users.id
        provider: 如 'github'

    用 SQLAlchemy delete() 语句直接删（不需要先 select），效率更高。
    """
    # delete(Model).where(...) 生成 DELETE FROM ... WHERE ... SQL
    await session.execute(
        delete(UserScmToken).where(
            UserScmToken.user_id == user_id,
            UserScmToken.provider == provider,
        )
    )
    # flush 确保 DELETE 在当前事务内立即生效（调用方再 commit 才持久化）
    await session.flush()
