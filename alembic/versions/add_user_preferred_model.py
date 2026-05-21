"""add users.preferred_model column

为多模型切换功能（qwen-plus / MiniMax-M2）持久化用户偏好。
nullable=True：老用户行迁移后值为 NULL → 后端读到 NULL 时走 DEFAULT_MODEL_ID 兜底。
不加 NOT NULL + default：避免锁表（大表 ALTER 风险）+ 应用层兜底足够。

Revision ID: add_user_preferred_model
Revises: s6_drop_memory_tables
Create Date: 2026-05-21
"""
# from __future__ import annotations 让类型注解延后求值（alembic 风格 stub）
from __future__ import annotations

# alembic op：DDL 操作接口；sqlalchemy 用来声明列类型
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "add_user_preferred_model"
down_revision = "s6_drop_memory_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """加 users.preferred_model VARCHAR(64) NULL。

    nullable=True 兼容老数据（无需 backfill）；应用层 (llm_factory.get_llm_provider)
    遇 NULL/未知值自动回退到 DEFAULT_MODEL_ID（KE_DEFAULT_MODEL env / qwen-plus）。
    """
    op.add_column(
        "users",
        sa.Column(
            # 列名与 SUPPORTED_MODELS 中 dict["id"] 同名；前端 / 后端 / DB 三层用同一 key
            "preferred_model",
            # VARCHAR(64) 足够：当前最长 model id 'MiniMax-M2' 才 10 字符；
            # 留余量供未来新增 vendor / 长 model id
            sa.String(length=64),
            # 老用户迁移后值为 NULL；应用层兜底，不破现有数据
            nullable=True,
            # 默认 NULL 等价于"未设置"；DEFAULT 走应用层 KE_DEFAULT_MODEL env
            # 不写 server_default 避免不同 MySQL 版本对 DDL DEFAULT 行为差异
        ),
    )


def downgrade() -> None:
    """回滚：DROP COLUMN preferred_model。

    应用层逻辑会因列消失而抛错 → 必须在 downgrade 前先把代码改回旧版（不读
    preferred_model）；属常规 schema 回滚操作。
    """
    op.drop_column("users", "preferred_model")
