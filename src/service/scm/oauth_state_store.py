# src/service/scm/oauth_state_store.py
"""OAuth/OIDC state/nonce 服务端存储：mint（含 CSRF cookie 值）+ 原子一次性消费 + GC。设计 §6 B1/B2/I10。"""
from __future__ import annotations  # 允许在类型注解中使用字符串形式的前向引用

import hashlib   # 标准库：提供哈希算法，此处用 sha256
import hmac      # 标准库：提供 HMAC 操作，compare_digest 用于常数时间比较（防时序攻击）
import secrets   # 标准库：提供密码学安全的随机字节生成，比 random 更安全
from dataclasses import dataclass    # 装饰器：自动生成 __init__/__repr__/__eq__ 等魔法方法
from datetime import datetime, timezone, timedelta  # 日期时间处理，timezone.utc 表示 UTC 时区
from typing import Optional          # 类型注解：Optional[X] 等价于 Union[X, None]

from sqlalchemy import delete, select           # SQLAlchemy Core：构建 DELETE/SELECT 语句
from sqlalchemy.ext.asyncio import AsyncSession  # 异步 SQLAlchemy session 类型

from src.service.db_models_homepage import OAuthState  # DB 模型：oauth_state 表映射

# state/csrf 默认有效期（秒）：10 分钟，和授权码流中 state 的常规有效窗口对齐
_DEFAULT_TTL = 600


def _sha256(v: str) -> str:
    """对字符串做 SHA-256 哈希，返回十六进制摘要（64 个字符）。

    Args:
        v: 要哈希的字符串
    Returns:
        64 位十六进制字符串
    """
    # encode("utf-8") 将 str → bytes；hexdigest() 返回人类可读的十六进制字符串
    return hashlib.sha256(v.encode("utf-8")).hexdigest()


@dataclass(frozen=True)  # frozen=True：创建不可变数据类（实例创建后不能修改字段）
class MintedState:
    """mint_state 的返回值，携带发给浏览器的**明文**凭证。DB 中只存它们的 sha256。"""
    state: str            # 明文，放进 authorize URL 的 state 参数
    csrf: str             # 明文，下发为 httponly cookie ke_oauth_csrf
    nonce: Optional[str]  # 明文，OIDC id_token 中的 nonce 值；不需要时为 None


async def mint_state(
    session: AsyncSession,
    *,                        # * 之后的参数必须以关键字形式传入（keyword-only arguments）
    provider: str,
    purpose: str,
    user_id: Optional[int],
    with_nonce: bool,
    ttl_seconds: int = _DEFAULT_TTL,
) -> MintedState:
    """生成 state + csrf（+可选 nonce），DB 只存它们的 sha256。返回明文给调用方下发。

    Args:
        session:     异步 SQLAlchemy session
        provider:    OAuth 提供商，例如 "github" / "gitlab"
        purpose:     使用目的，"login"（新登录）或 "link"（绑定已有账号）
        user_id:     link 场景下的发起者用户 ID；login 时为 None
        with_nonce:  是否生成 OIDC nonce（GitLab OIDC 流程需要，GitHub 不需要）
        ttl_seconds: state 有效期（秒），默认 10 分钟；测试时传 -1 使之立即过期
    Returns:
        MintedState 数据类，包含明文 state/csrf/nonce
    """
    # secrets.token_urlsafe(n) 生成 n 字节的随机 URL-safe Base64 字符串
    # 32 字节 = 256 位熵，满足 OAuth 2.0 对 state 不可预测性的要求
    state = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    # with_nonce 为 True 时才生成 nonce，避免不必要地写入 DB
    nonce = secrets.token_urlsafe(24) if with_nonce else None

    # 构建 OAuthState ORM 对象，DB 主键存 sha256(state)，不存明文（防 DB 泄漏后重放）
    row = OAuthState(
        state_hash=_sha256(state),   # 主键：sha256(state)
        csrf_hash=_sha256(csrf),     # 列：sha256(csrf cookie)
        provider=provider,
        purpose=purpose,
        user_id=user_id,
        nonce=nonce,
        # timedelta(seconds=ttl_seconds)：构造时间差；传 -1 则 expires_at 在过去（立即过期）
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
    )
    session.add(row)       # 将对象加入 session 追踪，标记为"待插入"
    await session.flush()  # flush：将 INSERT 发送到 DB 但不 commit（保持事务可回滚）
    return MintedState(state=state, csrf=csrf, nonce=nonce)


async def consume_state(
    session: AsyncSession,
    *,
    state: str,
    csrf: Optional[str],
) -> Optional[OAuthState]:
    """原子一次性消费（可移植：不依赖 DELETE…RETURNING，兼容 Oracle MySQL）。
    先读行（拿 nonce/purpose/user_id），再按 hash 删除并以 rowcount==1 作为并发单赢门。
    不存在/已用/过期/CSRF 不符 → None。必须在任何 token 交换/写入之前调用，同一事务内。

    Args:
        session: 异步 SQLAlchemy session
        state:   浏览器回调带回的明文 state 参数
        csrf:    从 httponly cookie 读取的明文 csrf 值
    Returns:
        成功时返回被删除的 OAuthState 行（含 provider/purpose/nonce 等）；失败时返回 None
    """
    # 防御性检查：csrf 为空直接拒绝（missing cookie = 可能 CSRF 攻击）
    if not csrf:
        return None

    # 计算 state 的 sha256，用于主键查找
    sh = _sha256(state)

    # 第一步：SELECT 读取行，拿到 nonce/purpose/user_id 等字段
    # 若行不存在（已消费 / 从未存在）→ 直接返回 None
    row = (await session.execute(
        select(OAuthState).where(OAuthState.state_hash == sh)
    )).scalar_one_or_none()
    if row is None:
        return None

    # 第二步：DELETE WHERE state_hash=sh，以 rowcount==1 作为原子单赢门
    #   - synchronize_session=False：跳过 ORM 内存对象同步；
    #     row 对象的属性在 delete 后仍可访问（我们只读不写），不需要 ORM 追踪该对象的删除状态
    #   - 并发下只有第一个 DELETE 能拿到 rowcount==1（DB 行锁保证唯一赢家）
    res = await session.execute(
        delete(OAuthState).where(OAuthState.state_hash == sh)
        .execution_options(synchronize_session=False)
    )
    if (res.rowcount or 0) != 1:
        return None  # 并发下被他人先消费（rowcount==0）

    # 检查是否过期：如果 expires_at 是 naive datetime（无时区），补上 UTC 标记
    # SQLite 不存时区信息，取出的 datetime 是 naive；MySQL 同样可能如此
    exp = row.expires_at
    if exp.tzinfo is None:
        # replace(tzinfo=...) 不转换时间值，只附加时区信息（不改变数值本身）
        exp = exp.replace(tzinfo=timezone.utc)
    if exp < datetime.now(timezone.utc):
        return None  # 已过期——行已被删，不会重放

    # CSRF 常数时间比较：
    #   hmac.compare_digest 在长度相同时用固定时间比较，防止时序侧信道攻击
    #   （普通字符串 == 比较会在第一个不匹配字符处提前返回，暴露时序信息）
    if not hmac.compare_digest(row.csrf_hash, _sha256(csrf)):
        return None  # CSRF 不匹配——行已被删，不会重放

    return row  # 验证通过，返回行数据供调用方使用（provider/purpose/nonce 等）


async def gc_expired(session: AsyncSession) -> int:
    """删除过期 state，返回删除数（回调机会式调用，防无界增长）。

    Args:
        session: 异步 SQLAlchemy session
    Returns:
        实际删除的行数
    """
    # DELETE WHERE expires_at < 当前时间：清理所有已过期但未被消费的行（如用户放弃授权流程）
    # synchronize_session=False：跳过 ORM 内存对象评估（SQLite 返回的 naive datetime
    # 与 timezone.utc aware datetime 比较会抛 TypeError），直接交给 DB 执行
    res = await session.execute(
        delete(OAuthState).where(OAuthState.expires_at < datetime.now(timezone.utc)),
        execution_options={"synchronize_session": False},
    )
    # rowcount：被影响的行数；若为 None（驱动不支持）则回退到 0
    return res.rowcount or 0
