"""homepage tables: projects, qa_sessions, qa_messages, qa_feedback, user_project_access

Revision ID: 3d539e6d585c
Revises: 0001
Create Date: 2026-05-06 17:11:08.281730

设计文档：[[首页设计]] §7.4

注：autogenerate 顺带检测到 users 表的 server_default / unique index 变化，
那些是 SQLAlchemy 版本差异的误报（不是真实改动），手动从迁移里剔除了，
只保留 5 张新表的 create_table。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3d539e6d585c'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """创建首页 5 张表。顺序：先父表（projects）再子表，避免 FK 引用未存在的表。"""

    # ─── 1. projects ────────────────────────────────────────────────────────
    op.create_table(
        'projects',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('repo_url', sa.String(length=512), nullable=True),
        sa.Column('language', sa.String(length=32), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('pipeline_at', sa.DateTime(), nullable=True),
        sa.Column('indexing_progress', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('created_by', sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_projects_status', 'projects', ['status'], unique=False)

    # ─── 2. user_project_access (FK→projects, FK→users) ─────────────────────
    op.create_table(
        'user_project_access',
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.String(length=64), nullable=False),
        sa.Column('role', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id', 'project_id'),
    )

    # ─── 3. qa_sessions (FK→projects) ───────────────────────────────────────
    op.create_table(
        'qa_sessions',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('project_id', sa.String(length=64), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('message_count', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'idx_qa_sessions_project_user',
        'qa_sessions',
        ['project_id', 'user_id', 'updated_at'],
        unique=False,
    )

    # ─── 4. qa_messages (FK→qa_sessions) ────────────────────────────────────
    op.create_table(
        'qa_messages',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('session_id', sa.String(length=64), nullable=False),
        sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('content', sa.Text(), nullable=True),
        sa.Column('sections', sa.JSON(), nullable=True),
        sa.Column('metadata', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.ForeignKeyConstraint(['session_id'], ['qa_sessions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_qa_messages_session', 'qa_messages', ['session_id', 'created_at'], unique=False)

    # ─── 5. qa_feedback (FK→qa_messages) ────────────────────────────────────
    op.create_table(
        'qa_feedback',
        sa.Column('message_id', sa.String(length=64), nullable=False),
        sa.Column('vote', sa.String(length=8), nullable=True),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.ForeignKeyConstraint(['message_id'], ['qa_messages.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('message_id'),
    )


def downgrade() -> None:
    """逆向：先删子表（依赖别人的）再删父表。"""
    op.drop_table('qa_feedback')
    op.drop_index('idx_qa_messages_session', table_name='qa_messages')
    op.drop_table('qa_messages')
    op.drop_index('idx_qa_sessions_project_user', table_name='qa_sessions')
    op.drop_table('qa_sessions')
    op.drop_table('user_project_access')
    op.drop_index('idx_projects_status', table_name='projects')
    op.drop_table('projects')
