"""验证 Fernet token 加密 / 解密 / hint 工具。"""
import pytest
from cryptography.fernet import Fernet

from src.service import token_crypto


@pytest.fixture(autouse=True)
def fresh_key(monkeypatch):
    """每个测试用一个新的 Fernet 密钥；clear lru_cache 确保 env 生效。"""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("KE_TOKEN_ENC_KEY", key)
    token_crypto.reset_fernet_cache()
    yield
    token_crypto.reset_fernet_cache()


# ───────── encrypt / decrypt ─────────

def test_roundtrip():
    plain = "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    cipher = token_crypto.encrypt_token(plain)
    assert cipher != plain
    assert token_crypto.decrypt_token(cipher) == plain


def test_encrypt_different_each_call():
    """Fernet 每次加密带随机 IV，相同明文密文也不同。"""
    plain = "secret"
    a = token_crypto.encrypt_token(plain)
    b = token_crypto.encrypt_token(plain)
    assert a != b
    assert token_crypto.decrypt_token(a) == plain
    assert token_crypto.decrypt_token(b) == plain


def test_encrypt_empty_rejected():
    with pytest.raises(ValueError):
        token_crypto.encrypt_token("")


def test_decrypt_empty_rejected():
    with pytest.raises(ValueError):
        token_crypto.decrypt_token("")


def test_decrypt_garbage_raises():
    """密文损坏或密钥不匹配 → ValueError（不暴露内部 InvalidToken）。"""
    with pytest.raises(ValueError):
        token_crypto.decrypt_token("not-a-valid-fernet-token")


def test_decrypt_wrong_key_raises(monkeypatch):
    """换密钥后再解原密文 → ValueError。"""
    cipher = token_crypto.encrypt_token("secret")
    new_key = Fernet.generate_key().decode()
    monkeypatch.setenv("KE_TOKEN_ENC_KEY", new_key)
    token_crypto.reset_fernet_cache()
    with pytest.raises(ValueError):
        token_crypto.decrypt_token(cipher)


# ───────── token_hint ─────────

def test_hint_normal():
    assert token_crypto.token_hint("ghp_xxxxxxxxxxxxxxxxabc") == "****xabc"


def test_hint_short():
    """短 token 不应崩溃，统一返 '****'。"""
    assert token_crypto.token_hint("ab") == "****"


def test_hint_empty():
    assert token_crypto.token_hint("") == "****"


# ───────── env 缺失 ─────────

def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("KE_TOKEN_ENC_KEY", raising=False)
    token_crypto.reset_fernet_cache()
    with pytest.raises(RuntimeError, match="KE_TOKEN_ENC_KEY"):
        token_crypto.encrypt_token("x")


def test_invalid_key_raises(monkeypatch):
    """非 Fernet 格式的 key → 启动时报错。"""
    monkeypatch.setenv("KE_TOKEN_ENC_KEY", "not-a-fernet-key")
    token_crypto.reset_fernet_cache()
    with pytest.raises(RuntimeError, match="格式不对"):
        token_crypto.encrypt_token("x")
