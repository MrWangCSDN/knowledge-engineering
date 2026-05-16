"""qa_sessions 加 title_custom 列

Revision ID: session_title_custom_v1
Revises: qa_archive_v1
Create Date: 2026-05-16

设计：[[会话标题-重命名与智能总结-设计]] §2
存量行 server_default=0（False），历史会话标题保持现状不回溯。
"""
from alembic import op
import sqlalchemy as sa

# Alembic 版本标识
revision = "session_title_custom_v1"
down_revision = "qa_archive_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "qa_sessions",
        sa.Column(
            "title_custom",
            sa.Boolean(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("qa_sessions", "title_custom")
