"""scm connection v1: scm_connections 表

Revision ID: scm_connection_v1
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "scm_connection_v1"
down_revision: Union[str, Sequence[str], None] = "225c95710efa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scm_connections",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("auth_type", sa.String(length=32), nullable=False),
        sa.Column("github_installation_id", sa.BigInteger(), nullable=True),
        sa.Column("account_login", sa.String(length=255), nullable=True),
        sa.Column("credential_id", sa.String(length=64), nullable=True),
        sa.Column("gitlab_instance_url", sa.String(length=512), nullable=True),
        sa.Column("oidc_issuer", sa.String(length=512), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("group_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["credential_id"], ["git_credentials.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="SET NULL"),
    )


def downgrade() -> None:
    op.drop_table("scm_connections")
