"""验证 Pydantic 校验。"""
import pytest
from pydantic import ValidationError
from src.service.auth_schemas import LoginRequest, MeResponse


def test_login_request_valid():
    req = LoginRequest(username="admin", password="12345678", remember_me=True)
    assert req.username == "admin"
    assert req.remember_me is True


def test_login_request_short_username():
    with pytest.raises(ValidationError):
        LoginRequest(username="ab", password="12345678")


def test_login_request_short_password():
    with pytest.raises(ValidationError):
        LoginRequest(username="admin", password="short")


def test_me_response_from_attributes():
    """from_attributes=True 让 MeResponse 能从 ORM 对象构造。"""
    from datetime import datetime
    class Fake:
        id = 1; email = "a@b.com"; username = "a"; is_admin = True
        created_at = datetime(2026, 5, 4)
    r = MeResponse.model_validate(Fake())
    assert r.id == 1 and r.email == "a@b.com"
