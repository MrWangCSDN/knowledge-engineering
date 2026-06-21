"""project scm binding v1: projects 加 SCM 绑定列

Revision ID: project_scm_binding_v1
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "project_scm_binding_v1"
down_revision: Union[str, Sequence[str], None] = "index_jobs_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # batch_alter_table：SQLite 不支持 ALTER ADD CONSTRAINT，用 batch 模式（同 59e1dde21b76 范式）
    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(sa.Column("scm_connection_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("repo_external_id", sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column("repo_full_name", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("ref", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("ref_type", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("subpath", sa.String(length=512), nullable=True))
        batch_op.create_foreign_key(
            "fk_projects_scm_connection", "scm_connections",
            ["scm_connection_id"], ["id"], ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_constraint("fk_projects_scm_connection", type_="foreignkey")
        for col in ("subpath", "ref_type", "ref", "repo_full_name", "repo_external_id", "scm_connection_id"):
            batch_op.drop_column(col)
