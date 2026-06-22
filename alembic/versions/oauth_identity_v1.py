"""oauth identity v1: users 身份列 + user_scm_token + oauth_state

Revision ID: oauth_identity_v1
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "oauth_identity_v1"
down_revision: Union[str, Sequence[str], None] = "project_scm_binding_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as b:
        b.add_column(sa.Column("github_user_id", sa.BigInteger(), nullable=True))
        b.add_column(sa.Column("gitlab_sub", sa.String(length=255), nullable=True))
        b.create_unique_constraint("uq_users_github_user_id", ["github_user_id"])
        b.create_unique_constraint("uq_users_gitlab_sub", ["gitlab_sub"])

    op.create_table(
        "user_scm_token",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("scopes", sa.Text(), nullable=True),
        sa.Column("scm_login", sa.String(length=255), nullable=True),
        sa.Column("linked_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.UniqueConstraint("user_id", "provider", name="uq_user_scm_token_user_provider"),
    )
    op.create_table(
        "oauth_state",
        sa.Column("state_hash", sa.String(length=64), primary_key=True),
        sa.Column("csrf_hash", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("nonce", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("oauth_state")
    op.drop_table("user_scm_token")
    with op.batch_alter_table("users") as b:
        b.drop_constraint("uq_users_gitlab_sub", type_="unique")
        b.drop_constraint("uq_users_github_user_id", type_="unique")
        b.drop_column("gitlab_sub")
        b.drop_column("github_user_id")
