"""首页相关 ORM 模型。

设计文档：[[首页设计]] §7.4

S6/S7 后保留的 ORM 类（qa_messages / qa_feedback / qa_user_memory /
qa_project_memory / qa_session_memory 已于 S6/S7 删除，消息与反馈改为
文件式存储；详见 [[文件式记忆重构-设计]] §7.6 (qa_*_memory + qa_messages
drop) + §8.4 (qa_feedback drop via S7)）：

  projects             - 工程元数据（id 是字符串，如 'deposit-system'）
  user_project_access  - 用户对工程的访问权限（v2 启用 RBAC，v1 写但不严格用）
  qa_sessions          - 问答会话（按工程归档；仅元数据，消息体在 fs）
  git_credentials      - Git 仓库访问凭证（PAT，Fernet 加密）

关键约定：
  - Project.id 用字符串（业务可读，如 'deposit-system'），不是自增 int
  - user_id 用 int，FK 到 users.id（auth_models.User）
  - 所有 FK 都开 CASCADE：删工程 → 删会话
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.service.db import Base


# ─── 1. projects ─────────────────────────────────────────────────────────────

class Project(Base):
    """工程元数据。一个企业可以有多个工程（每个 Java 微服务/模块一个）。"""
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    """工程 ID，业务可读字符串，如 'deposit-system'。"""

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    """工程显示名，如 '存款系统'。"""

    repo_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    """[deprecated] 旧字段，建议迁到 git_url。保留以兼容已有数据。"""

    language: Mapped[str] = mapped_column(String(32), default="java", nullable=False)
    """主语言，目前只支持 java。"""

    status: Mapped[str] = mapped_column(String(32), default="indexing", nullable=False)
    """状态：configured / indexing / ready / partial / failed。

    v1.0 仓库管理新增 'configured' 表示已配 git，但 pipeline 还没跑（v1.0 不做实际 clone）。
    """

    pipeline_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    """上次 pipeline 跑完的时间。indexing 时为 NULL。"""

    indexing_progress: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    """索引进度 JSON（phase/percent/eta_seconds）+ 解读完成度统计。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    """创建人 username（人类可读，不是 FK）。"""

    # ─── 仓库管理 v1.0 新增字段 ─────────────────────────────────
    git_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    """Git 仓库 URL，如 https://gitlab.bank.com/dep/core 或 git@host:path.git"""

    git_branch: Mapped[str] = mapped_column(String(128), default="main", nullable=False)
    """要跟踪的分支名，默认 main。"""

    git_credential_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        ForeignKey("git_credentials.id", ondelete="SET NULL"),
        nullable=True,
    )
    """关联到 git_credentials 表；NULL 表示公开仓库不需凭证。"""

    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    """最后一次成功 git fetch 的时间。"""

    last_synced_commit: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    """最后一次同步的 commit hash（前 40 字符 sha1）。"""

    sync_schedule: Mapped[str] = mapped_column(String(32), default="manual", nullable=False)
    """同步频率：manual / hourly / daily。v1.0 只支持 manual。"""

    repo_local_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    """项目源码在后端服务器的本地绝对路径；NULL 表示未配置。

    用于 4 个文件类工具（ke_grep / ke_glob / ke_read_file / ke_ls）拼路径。
    设计：[[代码源文件查询工具-设计]] §6
    """

    # ─── v2.0 多租户新增字段 ──────────────────────────────────────
    # group_id：工程所属的分组（FK → groups.id）
    # nullable=True：存量工程可以不属于任何 group，逐步迁移
    # ondelete='SET NULL'：组被删除时，工程的 group_id 置 NULL，工程本身不删除
    group_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        ForeignKey("groups.id", ondelete="SET NULL"),  # 删 group 不级联删工程
        nullable=True,  # 允许工程不归属任何 group（存量数据兼容）
    )

    __table_args__ = (
        Index("idx_projects_status", "status"),
    )


class GitCredential(Base):
    """Git 仓库访问凭证（PAT / 未来 SSH Key）。

    凭证内容用 Fernet 对称加密；密钥从 KE_TOKEN_ENC_KEY env 读取，不入库。
    UI 上只展示 token_hint（末 4 位），原文永远不返回前端。
    """
    __tablename__ = "git_credentials"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    """凭证 ID，如 'cred_abc123'。"""

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    """人类可读名称，如 'My GitLab PAT'。"""

    type: Mapped[str] = mapped_column(String(32), default="pat", nullable=False)
    """凭证类型。v1.0 只支持 'pat'，v2 可加 'ssh_key'。"""

    encrypted_token: Mapped[str] = mapped_column(Text, nullable=False)
    """Fernet 加密后的 token 密文。解密见 token_crypto.decrypt_token。"""

    token_hint: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    """末 4 位明文（如 '****abc'），仅 UI 展示用。"""

    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    """创建人 username。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    """最后一次被 fetch / ls-remote 使用的时间。"""

    # v2.0 新增：凭证归属用户
    # 暂时 nullable=True（迁移期间允许空）；Task 6 数据迁移完之后改 NOT NULL
    # ondelete='SET NULL'：用户注销后凭证保留（避免误删 + 配合审计）
    owner_user_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )


class ScmConnection(Base):
    """账号级 SCM 连接（"连接一次"）。设计 GitHub仓库连接-设计.md §5.1。

    github_app：github_installation_id 必填、credential_id 空；
    pat：credential_id 指向 git_credentials、github_installation_id 空。
    """
    __tablename__ = "scm_connections"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    auth_type: Mapped[str] = mapped_column(String(32), nullable=False)
    github_installation_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    account_login: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    credential_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("git_credentials.id", ondelete="SET NULL"), nullable=True
    )
    gitlab_instance_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    oidc_issuer: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    group_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("groups.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=text("(CURRENT_TIMESTAMP)"), nullable=False
    )


# ─── 2. user_project_access ──────────────────────────────────────────────────

class UserProjectAccess(Base):
    """用户对工程的访问权限。v1 不严格用（全员可见），v2 启用 RBAC。"""
    __tablename__ = "user_project_access"

    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    """用户 ID（int FK → users.id，auth_models.User）。"""

    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )

    role: Mapped[str] = mapped_column(String(32), default="reporter", nullable=False)
    """角色：reporter / maintainer / owner（v2.0 GitLab 风格三级）。

    v1 旧值（reader/writer/admin）保留向后兼容，但 v2 不再产出。
    migration v2b_remap_role 会把存量 reader→reporter / writer→maintainer / admin→owner 迁移。
    """


# ─── 3. qa_sessions ──────────────────────────────────────────────────────────

class QASession(Base):
    """问答会话。每次 + 新对话 创建一个 session，后续追问加 message。"""
    __tablename__ = "qa_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    """会话 ID，如 'sess_abc123'（前端/后端生成 uuid）。"""

    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    """会话归属的工程。删工程级联删会话。"""

    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    """会话所有者（int，对应 users.id；不加 FK 是为了保留已删用户的历史）。"""

    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    """会话标题（自动从首条消息生成；用户可改）。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    """消息数（缓存值，便于列表页展示，不用每次 join）。"""

    archived_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, default=None
    )
    """归档时间。NULL = 活动会话（默认）；非 NULL = 已归档（值即归档时刻，归档列表按此倒序）。
    设计：[[会话归档-设计]] §4.1。"""

    title_custom: Mapped[bool] = mapped_column(
        Boolean, server_default=text("0"), nullable=False, default=False
    )
    """标题是否被用户手动重命名过。
    False = 系统生成（截断 or 异步总结），可被异步总结覆盖；
    True  = 用户手动 rename 过，异步总结跳过、永不覆盖。
    设计：[[会话标题-重命名与智能总结-设计]] §2。"""

    __table_args__ = (
        # 主查询：左栏会话历史，按 project_id + user_id 过滤、按 updated_at 倒序
        Index(
            "idx_qa_sessions_project_user",
            "project_id", "user_id", "updated_at",
        ),
    )

