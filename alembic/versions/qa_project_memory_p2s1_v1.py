"""工程级记忆 S1：建 qa_project_memory 表

Revision ID: qa_project_memory_p2s1_v1
Revises: qa_memory_p1_v1
Create Date: 2026-05-17

设计：[[记忆系统-设计]] §19（S1 仅此表；S2-S4 列留空）
"""
from alembic import op
import sqlalchemy as sa

revision = "qa_project_memory_p2s1_v1"
down_revision = "qa_memory_p1_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "qa_project_memory",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(32), nullable=False, server_default=sa.text("'private'")),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.String(128), nullable=True),
        sa.Column("entity_kind", sa.String(32), nullable=True),
        sa.Column("grounding_status", sa.String(32), nullable=False, server_default=sa.text("'ungrounded'")),
        sa.Column("source", sa.String(32), nullable=False, server_default=sa.text("'explicit'")),
        sa.Column("source_session_id", sa.String(64), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False, server_default=sa.text("1.0")),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'active'")),
        sa.Column("promoted_by", sa.Integer(), nullable=True),
        sa.Column("promoted_at", sa.DateTime(), nullable=True),
        sa.Column("vector_synced", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_verified_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
    )
    op.create_index("idx_proj_scope", "qa_project_memory", ["project_id", "scope", "status"])
    op.create_index("idx_proj_user", "qa_project_memory", ["project_id", "user_id", "status"])
    op.create_index("idx_entity", "qa_project_memory", ["project_id", "entity_id"])


def downgrade() -> None:
    op.drop_index("idx_entity", table_name="qa_project_memory")
    op.drop_index("idx_proj_user", table_name="qa_project_memory")
    op.drop_index("idx_proj_scope", table_name="qa_project_memory")
    op.drop_table("qa_project_memory")
