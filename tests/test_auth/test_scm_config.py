"""GitHub App 配置从 env 读取测试。"""
import pytest
from src.service.scm.config import load_github_app_config, GitHubAppConfig


def test_load_from_env(monkeypatch, tmp_path):
    pem = tmp_path / "app.pem"
    pem.write_text("-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----\n")
    monkeypatch.setenv("KE_GH_APP_ID", "999")
    monkeypatch.setenv("KE_GH_APP_PRIVATE_KEY_PATH", str(pem))
    monkeypatch.setenv("KE_GH_WEBHOOK_SECRET", "whsec")

    cfg = load_github_app_config()
    assert isinstance(cfg, GitHubAppConfig)
    assert cfg.app_id == "999"
    assert "BEGIN RSA PRIVATE KEY" in cfg.private_key_pem
    assert cfg.webhook_secret == "whsec"


def test_missing_app_id_raises(monkeypatch):
    monkeypatch.delenv("KE_GH_APP_ID", raising=False)
    with pytest.raises(RuntimeError, match="KE_GH_APP_ID"):
        load_github_app_config()
