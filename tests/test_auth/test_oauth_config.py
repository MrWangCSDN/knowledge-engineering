# tests/test_auth/test_oauth_config.py
import pytest
from src.service.scm.config import load_oauth_config


def test_github_configured(monkeypatch):
    monkeypatch.setenv("KE_OAUTH_REDIRECT_BASE", "https://ke.example.com")
    monkeypatch.setenv("KE_GH_OAUTH_CLIENT_ID", "gh-cid")
    monkeypatch.setenv("KE_GH_OAUTH_CLIENT_SECRET", "gh-sec")
    monkeypatch.delenv("KE_GITLAB_OIDC_ISSUER", raising=False)
    cfg = load_oauth_config()
    assert cfg.redirect_base == "https://ke.example.com"
    assert cfg.github is not None and cfg.github.client_id == "gh-cid"
    assert cfg.gitlab is None            # 未配 → None（fail-closed）


def test_gitlab_configured(monkeypatch):
    monkeypatch.setenv("KE_OAUTH_REDIRECT_BASE", "https://ke.example.com")
    monkeypatch.delenv("KE_GH_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.setenv("KE_GITLAB_OIDC_ISSUER", "https://gitlab.example.com")
    monkeypatch.setenv("KE_GITLAB_OIDC_CLIENT_ID", "gl-cid")
    monkeypatch.setenv("KE_GITLAB_OIDC_CLIENT_SECRET", "gl-sec")
    cfg = load_oauth_config()
    assert cfg.github is None
    assert cfg.gitlab is not None and cfg.gitlab.issuer == "https://gitlab.example.com"


def test_provider_for_helper(monkeypatch):
    monkeypatch.setenv("KE_OAUTH_REDIRECT_BASE", "https://ke.example.com")
    monkeypatch.setenv("KE_GH_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("KE_GH_OAUTH_CLIENT_SECRET", "y")
    monkeypatch.delenv("KE_GITLAB_OIDC_ISSUER", raising=False)
    cfg = load_oauth_config()
    assert cfg.provider("github") is cfg.github
    assert cfg.provider("gitlab") is None
