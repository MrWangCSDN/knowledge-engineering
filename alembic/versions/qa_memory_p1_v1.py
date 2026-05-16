"""记忆系统 P1：建 qa_user_memory + qa_session_memory 两表

Revision ID: qa_memory_p1_v1
Revises: session_title_custom_v1
Create Date: 2026-05-16

设计：[[记忆系统-设计]] §5（P1 仅这两表，工程级 P2 再加）
"""
from alembic import op
import sqlalchemy as sa

# Alembic 版本标识
revision = "qa_memory_p1_v1"
down_revision = "session_title_custom_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "qa_user_memory",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False, server_default=sa.text("'explicit'")),
        sa.Column("source_session_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'active'")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "idx_qa_user_memory_user_active", "qa_user_memory", ["user_id", "status"]
    )
    op.create_table(
        "qa_session_memory",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.String(64), nullable=False, unique=True),
        sa.Column("working_summary", sa.Text(), nullable=False),
        sa.Column("focus_entity_ids", sa.JSON(), nullable=True),
        sa.Column("turn_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"], ["qa_sessions.id"], ondelete="CASCADE"
        ),
    )


def downgrade() -> None:
    op.drop_table("qa_session_memory")
    op.drop_index("idx_qa_user_memory_user_active", table_name="qa_user_memory")
    op.drop_table("qa_user_memory")
