"""GitHub App 配置：私钥从受保护文件读、其余从 env。设计 §7/§11。
绝不硬编码；私钥 PEM 路径走 KE_GH_APP_PRIVATE_KEY_PATH（chmod 600）。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


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


@dataclass(frozen=True)
class GitHubOAuthConfig:
    # GitHub OAuth App 凭证：client_id / client_secret 来自 GitHub OAuth App 设置页
    client_id: str
    client_secret: str


@dataclass(frozen=True)
class GitLabOidcConfig:
    # GitLab OIDC：issuer 形如 https://gitlab.example.com（discovery endpoint: {issuer}/.well-known/openid-configuration）
    issuer: str
    client_id: str
    client_secret: str


@dataclass(frozen=True)
class OAuthConfig:
    # 整体 OAuth 配置；某 provider env 未配 → 对应字段为 None（路由层 fail-closed 503）
    redirect_base: str                           # 来自 KE_OAUTH_REDIRECT_BASE，callback URL 前缀
    github: Optional[GitHubOAuthConfig]          # 未配置 KE_GH_OAUTH_CLIENT_ID/SECRET → None
    gitlab: Optional[GitLabOidcConfig]           # 未配置 KE_GITLAB_OIDC_* → None

    def provider(self, name: str) -> "Optional[object]":
        """按名取 provider 配置；未配置返回 None（路由层据此 fail-closed 503）。"""
        # dict.get 在 key 不存在时返回 None，与 provider 未配置语义一致
        return {"github": self.github, "gitlab": self.gitlab}.get(name)


def load_oauth_config() -> OAuthConfig:
    """从 env 装配 OAuth 配置。某 provider 凭证缺失 → 该 provider 为 None（不抛，路由层 503）。"""
    # KE_OAUTH_REDIRECT_BASE 是 callback URL 的域名前缀，如 https://ke.example.com
    # rstrip("/")：去掉末尾斜杠，防止拼接 redirect_uri 时产生双斜杠（如 "https://ke.example.com//auth/..."）
    redirect_base = os.getenv("KE_OAUTH_REDIRECT_BASE", "").strip().rstrip("/")
    # GitHub OAuth：client_id + client_secret 均有才视为已配置
    gh_id = os.getenv("KE_GH_OAUTH_CLIENT_ID", "").strip()
    gh_sec = os.getenv("KE_GH_OAUTH_CLIENT_SECRET", "").strip()
    # 条件表达式：两个字段都非空才构建配置，否则为 None（fail-closed）
    github = GitHubOAuthConfig(gh_id, gh_sec) if (gh_id and gh_sec) else None
    # GitLab OIDC：issuer + client_id + client_secret 均有才视为已配置
    gl_iss = os.getenv("KE_GITLAB_OIDC_ISSUER", "").strip()
    gl_id = os.getenv("KE_GITLAB_OIDC_CLIENT_ID", "").strip()
    gl_sec = os.getenv("KE_GITLAB_OIDC_CLIENT_SECRET", "").strip()
    gitlab = GitLabOidcConfig(gl_iss, gl_id, gl_sec) if (gl_iss and gl_id and gl_sec) else None
    return OAuthConfig(redirect_base=redirect_base, github=github, gitlab=gitlab)
