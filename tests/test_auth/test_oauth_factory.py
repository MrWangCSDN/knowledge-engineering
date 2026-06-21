# tests/test_auth/test_oauth_factory.py
"""测试 oauth_factory.get_login_provider 的路由逻辑（fail-closed 行为）。"""
import pytest  # pytest：Python 测试框架，pytest.raises 用于断言异常

# 导入 OAuthConfig 及其子配置，用于构造测试用配置对象
from src.service.scm.config import OAuthConfig, GitHubOAuthConfig, GitLabOidcConfig

# 被测函数：get_login_provider 按 provider 名返回登录 provider 实例
from src.service.scm.oauth_factory import get_login_provider, OAuthProviderUnavailable

# GitHubAppProvider：GitHub 登录 provider 类，用于 isinstance 断言
from src.service.scm.github_app import GitHubAppProvider

# GitLabOidcProvider：GitLab OIDC 登录 provider 类，用于 isinstance 断言
from src.service.scm.gitlab_oidc import GitLabOidcProvider


def _cfg(github: bool = True, gitlab: bool = True) -> OAuthConfig:
    """
    构造测试用 OAuthConfig。

    Args:
        github: 是否包含 GitHub OAuth 配置（False → github 字段为 None）
        gitlab: 是否包含 GitLab OIDC 配置（False → gitlab 字段为 None）
    Returns:
        OAuthConfig 实例
    """
    # 条件表达式：github=True 时构建 GitHubOAuthConfig，否则为 None（模拟未配置）
    return OAuthConfig(
        redirect_base="https://ke",
        github=GitHubOAuthConfig("gid", "gsec") if github else None,
        # 条件表达式：gitlab=True 时构建 GitLabOidcConfig，否则为 None
        gitlab=GitLabOidcConfig("https://gl", "lid", "lsec") if gitlab else None,
    )


def test_github(monkeypatch):
    """github provider 已配置时，get_login_provider 应返回 GitHubAppProvider 实例。"""
    # monkeypatch.setenv：在本测试作用域内设置环境变量，测试结束后自动还原
    # KE_GH_APP_ID 是 GitHub App 的数字 ID（load_github_app_config 需要）
    monkeypatch.setenv("KE_GH_APP_ID", "1")
    # /dev/null 读取结果为空串——load_github_app_config 不校验内容，仅要求路径可读
    monkeypatch.setenv("KE_GH_APP_PRIVATE_KEY_PATH", "/dev/null")
    # 调用被测函数：provider="github"，传入含 github 配置的 OAuthConfig
    p = get_login_provider("github", _cfg())
    # isinstance(obj, cls)：断言 obj 是 cls 的实例（或子类实例）
    assert isinstance(p, GitHubAppProvider)


def test_gitlab():
    """gitlab provider 已配置时，get_login_provider 应返回 GitLabOidcProvider 实例。"""
    # GitLab 分支不依赖 GitHub App env，直接调用
    p = get_login_provider("gitlab", _cfg())
    assert isinstance(p, GitLabOidcProvider)


def test_unconfigured_raises():
    """provider 在 OAuthConfig 中为 None（未配置）时，应抛出 OAuthProviderUnavailable。"""
    # pytest.raises 是上下文管理器：with 块内抛出指定异常则测试通过，否则失败
    with pytest.raises(OAuthProviderUnavailable):
        # gitlab=False → OAuthConfig.gitlab=None → factory 应 fail-closed 抛异常
        get_login_provider("gitlab", _cfg(gitlab=False))


def test_unknown_raises():
    """不支持的 provider 名称应抛出 OAuthProviderUnavailable（而非 KeyError/AttributeError）。"""
    with pytest.raises(OAuthProviderUnavailable):
        # "bitbucket" 不在支持列表中，factory 应统一抛 OAuthProviderUnavailable
        get_login_provider("bitbucket", _cfg())
