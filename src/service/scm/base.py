# src/service/scm/base.py
"""SCM Provider 抽象：业务层只依赖此处的 Protocol + 数据类 + 枚举，
GitHub/GitLab 各实现一份。设计 GitHub仓库连接-设计.md §6 / 身份与授权模型-设计.md。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Protocol


class ScmRole(str, Enum):
    """SCM 角色翻译到 KE 内部三档枚举（业务层只认这个）。"""
    CAN_BIND = "can_bind"        # 可绑定/连接仓库（owner/maintainer/admin）
    CAN_QUERY = "can_query"      # 可查询问答（read 及以上）
    NOT_VISIBLE = "not_visible"  # 不可见


@dataclass(frozen=True)
class RepoInfo:
    external_id: int          # 仓库 numeric id（绑定主键，rename/transfer 不变）
    full_name: str            # owner/repo（展示）
    default_branch: str
    private: bool = False


@dataclass(frozen=True)
class BranchList:
    default_branch: str
    branches: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ScmIdentity:
    provider: str             # github / gitlab
    scm_user_id: str          # numeric id（GitHub）/ sub（GitLab），账号关联主键
    login: Optional[str] = None


@dataclass(frozen=True)
class WebhookEvent:
    event_type: str           # push / create / installation / ...
    repo_external_id: Optional[int] = None
    ref: Optional[str] = None
    after_sha: Optional[str] = None
    delivery_id: Optional[str] = None


class ScmProvider(Protocol):
    """各 SCM 实现的统一接口。Plan 1 先实 GitHubAppProvider 的列仓/列分支/clone；
    身份与授权方法（get_login_identity/resolve_scm_role/list_user_visible_repos）在 Plan 4。"""

    async def list_repos(self, installation_id: int) -> list[RepoInfo]: ...
    async def list_branches(self, installation_id: int, full_name: str) -> BranchList: ...
    async def clone(self, installation_id: int, full_name: str, ref: str,
                    subpath: Optional[str], dest: str) -> str: ...
