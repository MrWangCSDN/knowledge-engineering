"""首页相关 ORM 模型（5 张表）。

设计文档：[[首页设计]] §7.4

5 张表：
  projects             - 工程元数据（id 是字符串，如 'deposit-system'）
  user_project_access  - 用户对工程的访问权限（v2 启用 RBAC，v1 写但不严格用）
  qa_sessions          - 问答会话（按工程归档）
  qa_messages          - 会话消息（user / assistant 两种 role）
  qa_feedback          - 用户反馈（👍 / 👎 + 可选评论）

关键约定：
  - Project.id 用字符串（业务可读，如 'deposit-system'），不是自增 int
  - user_id 用 int，FK 到 users.id（auth_models.User）
  - qa_messages.metadata 列在 SQLAlchemy 的 Python 属性上叫 msg_metadata（避开 DeclarativeBase.metadata）
  - 所有 FK 都开 CASCADE：删工程 → 删会话 → 删消息 → 删反馈
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
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

    # 关系定义（仅 Python 端，方便代码里写 sess.messages 而不需要再查）
    messages: Mapped[list["QAMessage"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="QAMessage.created_at",
    )


# ─── 4. qa_messages ──────────────────────────────────────────────────────────

class QAMessage(Base):
    """会话消息。一个 session 包含多条 message（user / assistant 交替）。"""
    __tablename__ = "qa_messages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    """消息 ID，如 'msg_xyz789'。"""

    session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("qa_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )

    role: Mapped[str] = mapped_column(String(16), nullable=False)
    """user / assistant。"""

    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    """user 消息：原始问题文本；assistant 消息：可选的 markdown 兜底文本。"""

    sections: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    """assistant 才有：6 段式结构化内容（Section[]）。"""

    # ⚠️ 列名是 'metadata'，但 Python 属性名必须是 msg_metadata，
    #    因为 DeclarativeBase 自带 metadata 属性（指向 SQLAlchemy MetaData 对象），会冲突。
    msg_metadata: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSON, nullable=True
    )
    """assistant 才有：entry_points / cited_entities / freshness / token_usage / latency_ms。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # 主查询：取一个 session 的全部消息，按 created_at 顺序展示
        Index("idx_qa_messages_session", "session_id", "created_at"),
    )

    session: Mapped["QASession"] = relationship(back_populates="messages")


# ─── 5. qa_feedback ──────────────────────────────────────────────────────────

class QAFeedback(Base):
    """用户对一条 assistant 消息的反馈。1 message ↔ 1 feedback（覆盖式更新）。"""
    __tablename__ = "qa_feedback"

    message_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("qa_messages.id", ondelete="CASCADE"),
        primary_key=True,
    )

    vote: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    """up / down / NULL（用户取消反馈）。"""

    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    """可选的文字反馈。"""

    user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    """反馈人 user.id。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


# ─── 6. qa_user_memory（记忆系统 P1：用户级软笔记）───────────────────────────
# 设计：[[记忆系统-设计]] §4.1。跨工程，量小，召回时全量注入 system prompt。

class QAUserMemory(Base):
    """用户级记忆：偏好 / 身份 / 风格反馈。跨所有工程，纯软笔记。"""
    __tablename__ = "qa_user_memory"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    """归属用户（对应 users.id）。与 QASession 一致不加 FK：保留已删用户的记忆。"""

    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    """preference / identity / style_feedback（沿用代码库 String+约定，不用 sa.Enum）。
    String(32) 留余量：'style_feedback' 已 14 字，16 无余量，有数据后改 schema 昂贵。"""

    content: Mapped[str] = mapped_column(Text, nullable=False)
    """自然语言软笔记，如『回答尽量简短』。"""

    source: Mapped[str] = mapped_column(String(32), nullable=False, default="explicit")
    """explicit（用户显式『记住…』）/ extracted（P2 异步抽取，P1 不产出）。"""

    source_session_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    """来源会话 ID（可追溯，不加 FK 硬绑）。"""

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    """active / archived（软删；遵守工程宪法禁物理删）。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        # 召回主查询：where user_id + status='active'
        Index("idx_qa_user_memory_user_active", "user_id", "status"),
    )


# ─── 7. qa_session_memory（记忆系统 P1：会话级工作状态）──────────────────────
# 设计：[[记忆系统-设计]] §4.3。一会话一行，滚动覆盖压缩摘要。

class QASessionMemory(Base):
    """会话级记忆：压缩后的工作状态。一对一绑定 QASession，覆盖式更新。"""
    __tablename__ = "qa_session_memory"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("qa_sessions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    """绑定的会话。删会话级联删其记忆。unique 保证一会话一行。"""

    working_summary: Mapped[str] = mapped_column(Text, nullable=False)
    """压缩后的工作状态（本次目标 / 已确认 / 已排除）。"""

    focus_entity_ids: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    """当前聚焦的 entity_id 列表（P1 可留空，为 P2/P3 预留）。"""

    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """上次压缩时的 message_count（用于判断是否需要再压缩）。"""

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
