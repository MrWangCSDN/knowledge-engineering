"""Git PAT 等敏感令牌的对称加密工具（Fernet / MultiFernet 密钥轮换）。

设计文档：[[仓库管理-设计]] §5

存：encrypted_token = encrypt_token(plain_pat)
取：plain_pat = decrypt_token(encrypted_token)

UI 展示：永远只用 token_hint(plain_pat) → '****abc'

环境变量（密钥轮换 P5c）：
  KE_TOKEN_ENC_KEYS   可选，逗号分隔的多 Fernet 密钥列表，**主 key 在最前**。
                       加密只用主 key；解密依次尝试所有 key（兼容旧密文）。
                       轮换流程：把新 key 放最前、旧 key 暂留其后，过渡期跑
                       re-encrypt 工具迁移全部密文，再下线旧 key。
  KE_TOKEN_ENC_KEY    回退（向后兼容），单个 44 字节 base64 编码的 Fernet 密钥。
                       仅当 KE_TOKEN_ENC_KEYS 未设置/为空时生效。
                       生成方式：
                         python -c "from cryptography.fernet import Fernet; \\
                                    print(Fernet.generate_key().decode())"

注意：
  - 密钥泄露 == 所有 PAT 泄露，必须严格保护
  - 测试环境用临时密钥即可
"""
from __future__ import annotations

import os
from functools import lru_cache

from cryptography.fernet import Fernet, MultiFernet, InvalidToken


def _load_keys() -> list[str]:
    """读密钥列表：优先 KE_TOKEN_ENC_KEYS（逗号分隔，主 key 在前），回退单 KE_TOKEN_ENC_KEY。"""
    multi = os.getenv("KE_TOKEN_ENC_KEYS", "")
    if multi.strip():
        keys = [k.strip() for k in multi.split(",") if k.strip()]
    else:
        single = os.getenv("KE_TOKEN_ENC_KEY", "").strip()
        keys = [single] if single else []
    if not keys:
        raise RuntimeError(
            "KE_TOKEN_ENC_KEYS / KE_TOKEN_ENC_KEY 未设置（至少一个 Fernet key）。"
            "生成：python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"")
    return keys


@lru_cache(maxsize=1)
def _get_multifernet() -> MultiFernet:
    """单例 MultiFernet。列表第一个=主 key（encrypt 用它）；decrypt 依次试所有 key。"""
    try:
        return MultiFernet([Fernet(k.encode("utf-8")) for k in _load_keys()])
    except (ValueError, TypeError) as e:
        raise RuntimeError(f"密钥格式不对（须 44 字节 base64 编码 Fernet key）：{e}") from e


def encrypt_token(plain: str) -> str:
    """加密明文 token，返回 base64 密文。

    用主 key（密钥列表第一个）加密。

    Args:
        plain: 明文 token，如 'ghp_xxxxxxxxxxxxxx'

    Returns:
        Fernet 密文，可直接存数据库 TEXT 字段。
    """
    if not plain:
        raise ValueError("token 不能为空")
    return _get_multifernet().encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_token(cipher: str) -> str:
    """解密密文，返回明文 token。

    MultiFernet 会依次尝试所有 key（含旧 key），任一成功即返回。

    Raises:
        ValueError: 密文损坏 / 所有密钥都不匹配（统一对外语义）
    """
    if not cipher:
        raise ValueError("密文不能为空")
    try:
        return _get_multifernet().decrypt(cipher.encode("utf-8")).decode("utf-8")
    except InvalidToken as e:
        raise ValueError("密文损坏或密钥不匹配") from e


def rotate_token(cipher: str) -> str:
    """用任一 key 解 → 主 key 重加密（MultiFernet.rotate）。re-encrypt 工具用。

    Args:
        cipher: 现有密文（可能由旧 key 加密）

    Returns:
        用主 key 重新加密后的密文。

    Raises:
        ValueError: 密文损坏 / 所有密钥都不匹配
    """
    if not cipher:
        raise ValueError("密文不能为空")
    try:
        return _get_multifernet().rotate(cipher.encode("utf-8")).decode("utf-8")
    except InvalidToken as e:
        raise ValueError("密文损坏或密钥不匹配") from e


def token_hint(plain: str) -> str:
    """生成 UI 展示用的 hint（末 4 位前补 ****）。

    用于在凭证列表里给 admin 看是哪条 PAT，但不暴露完整内容。
    """
    if not plain:
        return "****"
    if len(plain) < 4:
        return "****"
    return f"****{plain[-4:]}"


def reset_fernet_cache() -> None:
    """单测专用：重置 lru_cache，让 monkeypatch 后的 env 生效。"""
    _get_multifernet.cache_clear()
