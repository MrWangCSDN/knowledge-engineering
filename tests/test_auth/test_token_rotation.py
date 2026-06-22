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


import pytest_asyncio
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select
from src.service.db_models_homepage import Base, GitCredential, UserScmToken


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


def test_rotate_token_idempotent_on_primary(monkeypatch):
    """rotate 一个已用主 key 加密的密文：仍合法、单独主 key 可解（幂等，仅 IV 变）。"""
    k_new = Fernet.generate_key().decode()
    monkeypatch.setenv("KE_TOKEN_ENC_KEYS", k_new)
    monkeypatch.delenv("KE_TOKEN_ENC_KEY", raising=False)
    token_crypto.reset_fernet_cache()
    c = token_crypto.encrypt_token("s")
    r = token_crypto.rotate_token(c)
    assert Fernet(k_new.encode()).decrypt(r.encode()).decode() == "s"


def test_rotate_token_corrupt_raises(monkeypatch):
    """rotate 解不开的密文 → ValueError（与 decrypt 对称契约）。"""
    monkeypatch.setenv("KE_TOKEN_ENC_KEYS", Fernet.generate_key().decode())
    monkeypatch.delenv("KE_TOKEN_ENC_KEY", raising=False)
    token_crypto.reset_fernet_cache()
    with pytest.raises(ValueError):
        token_crypto.rotate_token("not-a-valid-fernet-token")


@pytest.mark.asyncio
async def test_reencrypt_all_tokens_migrates_to_primary(maker, monkeypatch):
    from src.service.token_reencrypt import reencrypt_all_tokens
    k_new = Fernet.generate_key().decode()
    k_old = Fernet.generate_key().decode()
    f_old = Fernet(k_old.encode())
    async with maker() as s:
        s.add(GitCredential(id="g1", name="c", encrypted_token=f_old.encrypt(b"pat1").decode()))
        s.add(UserScmToken(id="t1", user_id=1, provider="github",
                           access_token=f_old.encrypt(b"at1").decode(),
                           refresh_token=f_old.encrypt(b"rt1").decode(),
                           linked_at=datetime.now(timezone.utc)))
        s.add(UserScmToken(id="t2", user_id=2, provider="github",
                           access_token=f_old.encrypt(b"at2").decode(),
                           refresh_token=None, linked_at=datetime.now(timezone.utc)))
        s.add(GitCredential(id="bad", name="x", encrypted_token="not-a-valid-fernet-token"))
        await s.commit()
    monkeypatch.setenv("KE_TOKEN_ENC_KEYS", f"{k_new},{k_old}")
    monkeypatch.delenv("KE_TOKEN_ENC_KEY", raising=False)
    token_crypto.reset_fernet_cache()
    async with maker() as s:
        counts = await reencrypt_all_tokens(s)
    assert counts == {"git_credentials": 1, "user_scm_token": 2, "errors": 1}
    # 弃旧 key：仅留 k_new，迁移过的行仍可解
    monkeypatch.setenv("KE_TOKEN_ENC_KEYS", k_new)
    token_crypto.reset_fernet_cache()
    async with maker() as s:
        g1 = (await s.execute(select(GitCredential).where(GitCredential.id == "g1"))).scalar_one()
        t1 = (await s.execute(select(UserScmToken).where(UserScmToken.id == "t1"))).scalar_one()
        t2 = (await s.execute(select(UserScmToken).where(UserScmToken.id == "t2"))).scalar_one()
        assert token_crypto.decrypt_token(g1.encrypted_token) == "pat1"
        assert token_crypto.decrypt_token(t1.access_token) == "at1"
        assert token_crypto.decrypt_token(t1.refresh_token) == "rt1"
        assert token_crypto.decrypt_token(t2.access_token) == "at2"
        assert t2.refresh_token is None
