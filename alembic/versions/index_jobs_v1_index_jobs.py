"""index jobs v1: index_jobs 表

Revision ID: index_jobs_v1
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "index_jobs_v1"
down_revision: Union[str, Sequence[str], None] = "scm_connection_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "index_jobs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("trigger", sa.String(length=32), nullable=False),
        sa.Column("commit_sha", sa.String(length=40), nullable=True),
        sa.Column("dedup_key", sa.String(length=128), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("worker_id", sa.String(length=64), nullable=True),
        sa.Column("progress", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.Index("idx_index_jobs_status", "status"),
    )


def downgrade() -> None:
    op.drop_table("index_jobs")
