"""index job lease v1: index_jobs 加 lease_expires（卡死作业 reaper 租约）

Revision ID: index_job_lease_v1
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "index_job_lease_v1"
down_revision: Union[str, Sequence[str], None] = "oauth_identity_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("index_jobs") as batch_op:
        batch_op.add_column(sa.Column("lease_expires", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("index_jobs") as batch_op:
        batch_op.drop_column("lease_expires")
