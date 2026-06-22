"""P5c token 密钥轮换：MultiFernet 多 key + rotate_token + reencrypt。"""
import pytest
from cryptography.fernet import Fernet
from src.service import token_crypto


@pytest.fixture(autouse=True)
def _reset():
    token_crypto.reset_fernet_cache()
    yield
    token_crypto.reset_fernet_cache()


def test_backward_compat_single_key(monkeypatch):
    monkeypatch.delenv("KE_TOKEN_ENC_KEYS", raising=False)
    monkeypatch.setenv("KE_TOKEN_ENC_KEY", Fernet.generate_key().decode())
    token_crypto.reset_fernet_cache()
    c = token_crypto.encrypt_token("ghp_secret")
    assert token_crypto.decrypt_token(c) == "ghp_secret"


def test_multi_key_encrypt_uses_primary(monkeypatch):
    k_new = Fernet.generate_key().decode()
    k_old = Fernet.generate_key().decode()
    monkeypatch.setenv("KE_TOKEN_ENC_KEYS", f"{k_new},{k_old}")
    monkeypatch.delenv("KE_TOKEN_ENC_KEY", raising=False)
    token_crypto.reset_fernet_cache()
    c = token_crypto.encrypt_token("s")
    assert Fernet(k_new.encode()).decrypt(c.encode()).decode() == "s"


def test_multi_key_decrypt_fallback_and_missing(monkeypatch):
    k_new = Fernet.generate_key().decode()
    k_old = Fernet.generate_key().decode()
    old_cipher = Fernet(k_old.encode()).encrypt(b"s").decode()
    monkeypatch.setenv("KE_TOKEN_ENC_KEYS", f"{k_new},{k_old}")
    monkeypatch.delenv("KE_TOKEN_ENC_KEY", raising=False)
    token_crypto.reset_fernet_cache()
    assert token_crypto.decrypt_token(old_cipher) == "s"
    monkeypatch.setenv("KE_TOKEN_ENC_KEYS", k_new)
    token_crypto.reset_fernet_cache()
    with pytest.raises(ValueError):
        token_crypto.decrypt_token(old_cipher)


def test_rotate_token_migrates_to_primary(monkeypatch):
    k_new = Fernet.generate_key().decode()
    k_old = Fernet.generate_key().decode()
    old_cipher = Fernet(k_old.encode()).encrypt(b"s").decode()
    monkeypatch.setenv("KE_TOKEN_ENC_KEYS", f"{k_new},{k_old}")
    monkeypatch.delenv("KE_TOKEN_ENC_KEY", raising=False)
    token_crypto.reset_fernet_cache()
    rotated = token_crypto.rotate_token(old_cipher)
    assert Fernet(k_new.encode()).decrypt(rotated.encode()).decode() == "s"


def test_empty_keys_raises(monkeypatch):
    monkeypatch.delenv("KE_TOKEN_ENC_KEYS", raising=False)
    monkeypatch.delenv("KE_TOKEN_ENC_KEY", raising=False)
    token_crypto.reset_fernet_cache()
    with pytest.raises(RuntimeError):
        token_crypto.encrypt_token("x")


def test_whitespace_keys_filtered(monkeypatch):
    k1 = Fernet.generate_key().decode()
    k2 = Fernet.generate_key().decode()
    monkeypatch.setenv("KE_TOKEN_ENC_KEYS", f"{k2},, ,{k1},")
    monkeypatch.delenv("KE_TOKEN_ENC_KEY", raising=False)
    token_crypto.reset_fernet_cache()
    c = token_crypto.encrypt_token("s")
    assert Fernet(k2.encode()).decrypt(c.encode()).decode() == "s"
