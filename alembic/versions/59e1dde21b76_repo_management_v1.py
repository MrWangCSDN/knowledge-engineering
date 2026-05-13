"""repo management v1: git fields + git_credentials table

Revision ID: 59e1dde21b76
Revises: 3d539e6d585c
Create Date: 2026-05-07

新增：
  · git_credentials 表（存 PAT 加密令牌）
  · projects 表新增 git 相关字段（git_url / git_branch / git_credential_id /
    last_synced_at / last_synced_commit / sync_schedule）

注：autogenerate 顺带误报 users 表 server_default / unique index 变化
（SQLAlchemy 内省差异），手动从迁移里剔除，只保留本次的真实改动。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '59e1dde21b76'
down_revision: Union[str, Sequence[str], None] = '3d539e6d585c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """创建 git_credentials + projects 加 git 字段。"""

    # ─── 1. git_credentials 表 ─────────────────────────────────────────
    op.create_table(
        'git_credentials',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('type', sa.String(length=32), nullable=False),
        sa.Column('encrypted_token', sa.Text(), nullable=False),
        sa.Column('token_hint', sa.String(length=16), nullable=True),
        sa.Column('created_by', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )

    # ─── 2. projects 表新增 git 字段 ──────────────────────────────────
    with op.batch_alter_table('projects') as batch_op:
        batch_op.add_column(sa.Column('git_url', sa.String(length=512), nullable=True))
        batch_op.add_column(sa.Column('git_branch', sa.String(length=128),
                                       nullable=False, server_default='main'))
        batch_op.add_column(sa.Column('git_credential_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('last_synced_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('last_synced_commit', sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column('sync_schedule', sa.String(length=32),
                                       nullable=False, server_default='manual'))
        batch_op.create_foreign_key(
            'fk_projects_git_credential',
            'git_credentials',
            ['git_credential_id'], ['id'],
            ondelete='SET NULL',
        )


def downgrade() -> None:
    """逆向：先删 projects 加的字段（含 FK），再删 git_credentials 表。"""

    with op.batch_alter_table('projects') as batch_op:
        batch_op.drop_constraint('fk_projects_git_credential', type_='foreignkey')
        batch_op.drop_column('sync_schedule')
        batch_op.drop_column('last_synced_commit')
        batch_op.drop_column('last_synced_at')
        batch_op.drop_column('git_credential_id')
        batch_op.drop_column('git_branch')
        batch_op.drop_column('git_url')

    op.drop_table('git_credentials')
