"""按 env 造 GitHubAppProvider 单例。测试用依赖覆盖注入 fake。"""
from __future__ import annotations

from functools import lru_cache

from src.service.scm.config import load_github_app_config
from src.service.scm.github_app import GitHubAppProvider


@lru_cache(maxsize=1)
def get_github_provider() -> GitHubAppProvider:
    return GitHubAppProvider(load_github_app_config())
