# src/service/scm/oauth_factory.py
"""按 provider 选择登录 provider（fail-closed：未配置 → OAuthProviderUnavailable → 路由 503）。设计 §4.5。"""
# __future__.annotations：允许在类型注解中使用尚未定义的类名（前向引用），Python 3.9 兼容
from __future__ import annotations

# 导入配置类：OAuthConfig 是整体 OAuth 配置，load_github_app_config 从 env 读取 GitHub App 私钥配置
from src.service.scm.config import OAuthConfig, load_github_app_config

# 导入 GitHub 登录 provider：GitHub 登录路径复用 App 私钥机制（签 JWT 获取 user-token）
from src.service.scm.github_app import GitHubAppProvider

# 导入 GitLab OIDC 登录 provider
from src.service.scm.gitlab_oidc import GitLabOidcProvider


class OAuthProviderUnavailable(Exception):
    """provider 未配置或不支持时抛出（路由层捕获后统一转 503，而非 500）。

    继承自 Exception（所有非系统退出异常的基类），是标准的自定义异常写法：
      class MyError(Exception): pass
    路由层使用 except OAuthProviderUnavailable → HTTPException(503)。
    """


def get_login_provider(provider: str, oauth_cfg: OAuthConfig) -> GitHubAppProvider | GitLabOidcProvider:
    """
    按 provider 名称返回对应的登录 provider 实例（fail-closed：任何缺失都抛异常）。

    Args:
        provider:  provider 名称，目前支持 "github" / "gitlab"
        oauth_cfg: 从 env 装配的 OAuth 配置（github/gitlab 字段为 None 表示未配置）
    Returns:
        GitHubAppProvider 或 GitLabOidcProvider 实例
    Raises:
        OAuthProviderUnavailable: provider 未配置、KE_GH_APP_* 缺失或 provider 名称不支持
    """
    # if-elif 链按 provider 名路由；不用 dict dispatch 是为了让每个分支的错误处理逻辑清晰可读
    if provider == "github":
        # fail-closed：oauth_cfg.github 为 None 说明 KE_GH_OAUTH_CLIENT_* env 未配置
        if oauth_cfg.github is None:
            raise OAuthProviderUnavailable("github OAuth 未配置")
        # GitHub 登录路径复用 GitHubAppProvider（需要 App 私钥签 JWT 换 user-token）
        # load_github_app_config 若 KE_GH_APP_ID / KE_GH_APP_PRIVATE_KEY_PATH 缺失则抛 RuntimeError
        # 这里用 try/except 把 RuntimeError 转换为 OAuthProviderUnavailable，
        # 让路由层统一 503（配置缺失是运维问题，不应向客户端暴露 500）
        try:
            app_cfg = load_github_app_config()
        except RuntimeError as e:
            # raise ... from e：保留原始异常链，方便调试（__cause__ 指向原 RuntimeError）
            raise OAuthProviderUnavailable(f"github App 配置缺失：{e}") from e
        # 用读取到的 App 配置构造 provider 实例
        return GitHubAppProvider(app_cfg)

    if provider == "gitlab":
        # fail-closed：oauth_cfg.gitlab 为 None 说明 KE_GITLAB_OIDC_* env 未配置
        if oauth_cfg.gitlab is None:
            raise OAuthProviderUnavailable("gitlab OIDC 未配置")
        # GitLabOidcProvider 只需 GitLabOidcConfig，无额外 env 依赖
        return GitLabOidcProvider(oauth_cfg.gitlab)

    # 走到这里说明 provider 名称不在支持列表中，统一抛 OAuthProviderUnavailable
    # f-string：f"..." 是 Python 格式化字符串，{provider} 会被变量值替换
    raise OAuthProviderUnavailable(f"不支持的 provider: {provider}")
