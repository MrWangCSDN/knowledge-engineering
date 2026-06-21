"""GitHub App 配置：私钥从受保护文件读、其余从 env。设计 §7/§11。
绝不硬编码；私钥 PEM 路径走 KE_GH_APP_PRIVATE_KEY_PATH（chmod 600）。"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class GitHubAppConfig:
    app_id: str
    private_key_pem: str
    webhook_secret: str


def load_github_app_config() -> GitHubAppConfig:
    app_id = os.getenv("KE_GH_APP_ID", "").strip()
    if not app_id:
        raise RuntimeError("KE_GH_APP_ID 未设置（GitHub App 连接需要）")
    pem_path = os.getenv("KE_GH_APP_PRIVATE_KEY_PATH", "").strip()
    if not pem_path:
        raise RuntimeError("KE_GH_APP_PRIVATE_KEY_PATH 未设置")
    try:
        with open(pem_path, "r", encoding="utf-8") as f:
            private_key_pem = f.read()
    except OSError as e:
        raise RuntimeError(f"读取 GitHub App 私钥失败（{pem_path}）：{e}") from e
    webhook_secret = os.getenv("KE_GH_WEBHOOK_SECRET", "").strip()
    return GitHubAppConfig(app_id=app_id, private_key_pem=private_key_pem, webhook_secret=webhook_secret)
