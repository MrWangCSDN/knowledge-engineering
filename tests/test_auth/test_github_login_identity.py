# tests/test_auth/test_github_login_identity.py
"""测试 GitHubAppProvider 的 OAuth 登录身份方法（build_authorize_url / get_login_identity）。
不依赖真实 GitHub API；使用 pytest-httpx 的 httpx_mock 拦截 HTTP 请求。"""

import pytest
# 导入被测试的 GitHubAppProvider 类
from src.service.scm.github_app import GitHubAppProvider
# 导入 GitHubApp 配置数据类
from src.service.scm.config import GitHubAppConfig
# 导入 SCM 身份数据类（用于断言返回值）
from src.service.scm.base import ScmIdentity


def _provider():
    """构造一个测试用 GitHubAppProvider 实例。
    登录身份路径不触发 App-JWT 签名，因此私钥给最小占位值即可（不实际用于签名）。"""
    # GitHubAppConfig 需要 app_id、private_key_pem、webhook_secret 三个字段
    return GitHubAppProvider(GitHubAppConfig(app_id="1", private_key_pem="x", webhook_secret=""))


def test_build_authorize_url():
    """验证 build_authorize_url 返回符合 GitHub App user-to-server 规范的 URL。
    不带 scope 参数；权限由 GitHub App 配置决定。"""
    # 创建 provider 实例
    p = _provider()
    # 调用构造授权 URL 的方法，传入 client_id、redirect_uri、state
    url = p.build_authorize_url(client_id="cid", redirect_uri="https://ke/cb", state="st")
    # 断言：URL 必须指向 GitHub OAuth 授权端点
    assert "github.com/login/oauth/authorize" in url
    # 断言：URL 必须包含 client_id、state、redirect_uri 三个查询参数
    assert "client_id=cid" in url and "state=st" in url and "redirect_uri=" in url


@pytest.mark.asyncio
async def test_get_login_identity(httpx_mock):
    """验证 get_login_identity 用 access_token 调 GET /user 并解析成 ScmIdentity。
    httpx_mock 是 pytest-httpx 提供的 fixture，拦截所有 httpx 请求并返回预设响应。"""
    # 预设：当请求 https://api.github.com/user 时，返回包含 id 和 login 的 JSON
    httpx_mock.add_response(url="https://api.github.com/user",
                            json={"id": 42, "login": "octocat"})
    # 创建 provider 实例
    p = _provider()
    # 调用 get_login_identity，传入含 access_token 的 token 字典
    # await 关键字：等待协程（async 函数）执行完毕，Python 异步编程的基本语法
    ident = await p.get_login_identity({"access_token": "AT"})
    # 断言：返回值必须是预期的 ScmIdentity dataclass 实例
    # ScmIdentity 是 frozen=True 的 dataclass，支持 == 按字段比较
    assert ident == ScmIdentity(provider="github", scm_user_id="42", login="octocat")
