"""Git PAT 等敏感令牌的对称加密工具（Fernet）。

设计文档：[[仓库管理-设计]] §5

存：encrypted_token = encrypt_token(plain_pat)
取：plain_pat = decrypt_token(encrypted_token)

UI 展示：永远只用 token_hint(plain_pat) → '****abc'

环境变量：
  KE_TOKEN_ENC_KEY    必填，44 字节 base64 编码的 Fernet 密钥
                       生成方式：
                         python -c "from cryptography.fernet import Fernet; \\
                                    print(Fernet.generate_key().decode())"

注意：
  - 密钥泄露 == 所有 PAT 泄露，必须严格保护
  - 轮换密钥需要 re-encrypt 所有现有凭证（v2 再做）
  - 测试环境用临时密钥即可
"""
from __future__ import annotations

import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken


@lru_cache(maxsize=1)
def _get_fernet() -> Fernet:
    """单例 Fernet 实例。lru_cache 让多次调用复用同一个对象。"""
    key = os.getenv("KE_TOKEN_ENC_KEY", "")
    if not key:
        raise RuntimeError(
            "KE_TOKEN_ENC_KEY 未设置。"
            "生成方式：python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
    try:
        return Fernet(key.encode("utf-8"))
    except (ValueError, TypeError) as e:
        raise RuntimeError(
            f"KE_TOKEN_ENC_KEY 格式不对（必须是 44 字节 base64 编码 Fernet key）：{e}"
        ) from e


def encrypt_token(plain: str) -> str:
    """加密明文 token，返回 base64 密文。

    Args:
        plain: 明文 token，如 'ghp_xxxxxxxxxxxxxx'

    Returns:
        Fernet 密文，可直接存数据库 TEXT 字段。
    """
    if not plain:
        raise ValueError("token 不能为空")
    return _get_fernet().encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_token(cipher: str) -> str:
    """解密密文，返回明文 token。

    Raises:
        ValueError: 密文损坏 / 密钥不对（统一对外语义）
    """
    if not cipher:
        raise ValueError("密文不能为空")
    try:
        return _get_fernet().decrypt(cipher.encode("utf-8")).decode("utf-8")
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
    _get_fernet.cache_clear()
