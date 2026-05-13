"""admin 相关 Pydantic schemas（仓库管理 v1.0）。

设计文档：[[仓库管理-设计]]
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


# ─── git 凭证 ─────────────────────────────────────────────────────────

class CredentialCreateRequest(BaseModel):
    """POST /admin/credentials body。"""
    name: str = Field(..., min_length=1, max_length=128, description="人类可读名，如 'My GitLab PAT'")
    type: Literal["pat"] = "pat"
    token: str = Field(..., min_length=8, max_length=512, description="明文 PAT；服务端会 Fernet 加密")


class CredentialResponse(BaseModel):
    """API 返回的凭证（永远不含 encrypted_token）。"""
    id: str
    name: str
    type: str
    token_hint: Optional[str]
    created_by: Optional[str]
    created_at: str
    last_used_at: Optional[str]


class CredentialListResponse(BaseModel):
    credentials: list[CredentialResponse]


# ─── 测试 git 连接 ────────────────────────────────────────────────────

class TestConnectionRequest(BaseModel):
    """POST /admin/projects/test-connection body。

    用于添加仓库前先验证 URL + 凭证是否有效。不写库。
    """
    git_url: str = Field(..., min_length=4, max_length=512)
    credential_id: Optional[str] = Field(None, description="可选；公开仓不用凭证")
    branch: Optional[str] = Field(None, description="留空走 HEAD（默认分支）")


class TestConnectionResponse(BaseModel):
    ok: bool
    default_branch: Optional[str] = None
    """从 ls-remote 解析出的默认分支名，如 'main'。"""
    last_commit: Optional[str] = None
    """指定分支或 HEAD 上的最后 commit hash。"""
    error: Optional[str] = None
    """ok=False 时的错误信息（已 mask 敏感内容）。"""


# ─── admin project CRUD ──────────────────────────────────────────────

class AdminProjectCreateRequest(BaseModel):
    """POST /admin/projects body — admin 创建工程并关联 Git。"""
    id: str = Field(
        ...,
        pattern=r"^[a-z][a-z0-9-]{0,62}[a-z0-9]$",
        description="工程 ID（如 'deposit-system'）",
    )
    name: str = Field(..., min_length=1, max_length=128)
    domain: Optional[str] = Field(None, max_length=64, description="所属域，如 'deposit'")
    git_url: str = Field(..., min_length=4, max_length=512)
    git_branch: str = Field("main", min_length=1, max_length=128)
    credential_id: Optional[str] = Field(None, description="git_credentials.id")
    language: str = Field("java")


class AdminProjectUpdateRequest(BaseModel):
    """PATCH /admin/projects/{id} body — 部分更新。"""
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    domain: Optional[str] = Field(None, max_length=64)
    git_url: Optional[str] = Field(None, min_length=4, max_length=512)
    git_branch: Optional[str] = Field(None, min_length=1, max_length=128)
    credential_id: Optional[str] = None


class AdminProjectResponse(BaseModel):
    """admin 视角的工程详情（含 git 配置）。"""
    id: str
    name: str
    domain: Optional[str] = None
    status: str
    git_url: Optional[str]
    git_branch: str
    credential_id: Optional[str]
    last_synced_at: Optional[str]
    last_synced_commit: Optional[str]
    sync_schedule: str
    created_at: str
    created_by: Optional[str]


class AdminProjectListResponse(BaseModel):
    projects: list[AdminProjectResponse]
