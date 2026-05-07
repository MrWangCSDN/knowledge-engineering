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
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    func,
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
    """git 仓库地址，可选。"""

    language: Mapped[str] = mapped_column(String(32), default="java", nullable=False)
    """主语言，目前只支持 java。"""

    status: Mapped[str] = mapped_column(String(32), default="indexing", nullable=False)
    """状态：ready / indexing / partial / failed。详见 spec §4.3。"""

    pipeline_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    """上次 pipeline 跑完的时间。indexing 时为 NULL。"""

    indexing_progress: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    """索引进度 JSON（phase/percent/eta_seconds）+ 解读完成度统计。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    """创建人 username（人类可读，不是 FK）。"""

    __table_args__ = (
        Index("idx_projects_status", "status"),
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

    role: Mapped[str] = mapped_column(String(32), default="reader", nullable=False)
    """角色：reader / writer / admin。"""


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
