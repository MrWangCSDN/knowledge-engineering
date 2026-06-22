"""User SQLAlchemy model（用于登录与认证）。

字段约定见 [[登录与认证-设计]] §3 数据模型。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from src.service.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
    # 用户偏好的 LLM 模型 id（如 'qwen-plus' / 'MiniMax-M2'）。
    # nullable=True：未设置时应用层（llm_factory.get_llm_provider）兜底用 DEFAULT_MODEL_ID。
    # 由 alembic add_user_preferred_model 添加；详见 llm_factory.SUPPORTED_MODELS。
    preferred_model: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # ── SCM 身份链（P4a，设计 §5）；身份键=SCM 数字 id，UNIQUE ──
    github_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, unique=True)
    gitlab_sub: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, unique=True)

    def __repr__(self) -> str:
        return f"<User id={self.id} username={self.username!r} admin={self.is_admin}>"
