"""验证 bcrypt + JWT + cookie 配置。"""
import time

import pytest

from src.service import auth_security as sec


@pytest.fixture(autouse=True)
def _set_secret(monkeypatch):
    monkeypatch.setenv("KE_JWT_SECRET", "x" * 32)


def test_hash_and_verify():
    pwd = "MySecretPwd!"
    h = sec.hash_password(pwd)
    assert h.startswith("$2")  # bcrypt 标识
    assert sec.verify_password(pwd, h) is True
    assert sec.verify_password("wrong", h) is False


def test_jwt_round_trip():
    tok = sec.create_access_token(user_id=42, username="alice")
    payload = sec.decode_token(tok)
    assert payload["sub"] == "42"
    assert payload["username"] == "alice"
    assert payload["type"] == "access"


def test_jwt_expired(monkeypatch):
    monkeypatch.setenv("KE_JWT_ACCESS_TTL_MIN", "0")  # 立即过期
    tok = sec.create_access_token(user_id=1, username="x")
    time.sleep(1)
    assert sec.decode_token(tok) is None


def test_refresh_token_jti_unique():
    a = sec.create_refresh_token(user_id=1, remember_me=False)
    b = sec.create_refresh_token(user_id=1, remember_me=False)
    pa = sec.decode_token(a); pb = sec.decode_token(b)
    assert pa["jti"] != pb["jti"]


def test_cookie_settings_remember_vs_short(monkeypatch):
    monkeypatch.setenv("KE_JWT_REFRESH_TTL_DAYS", "1")
    monkeypatch.setenv("KE_JWT_REFRESH_TTL_REMEMBER_DAYS", "7")
    short = sec.cookie_settings(remember_me=False)
    long_ = sec.cookie_settings(remember_me=True)
    assert short["max_age"] == 86400
    assert long_["max_age"] == 7 * 86400
    assert short["httponly"] is True
    assert short["path"] == "/api/auth"


def test_missing_secret(monkeypatch):
    monkeypatch.delenv("KE_JWT_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="≥ 32"):
        sec.create_access_token(user_id=1, username="x")
