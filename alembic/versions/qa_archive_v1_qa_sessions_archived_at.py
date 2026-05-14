"""qa_archive_v1: qa_sessions add archived_at + composite index.

设计文档：[[会话归档-设计]] §4.1 / §4.3

新增：
  · qa_sessions.archived_at DATETIME NULL（NULL = 活动，非 NULL = 已归档）
  · idx_qa_sessions_user_archived 复合索引 (user_id, archived_at)
    覆盖"当前用户全部归档"主查询路径

Revision ID: qa_archive_v1
Revises: v2b_remap_role
Create Date: 2026-05-13
"""
# 从 alembic 包导入 op（Alembic 的 DDL 操作对象）
from alembic import op
# 导入 sqlalchemy 供类型声明使用
import sqlalchemy as sa


# Alembic 的版本标识。revision = 当前版本；down_revision = 上一个版本
revision = 'qa_archive_v1'
down_revision = 'v2b_remap_role'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """加 archived_at 字段 + 复合索引。"""
    # batch_alter_table: 在 sqlite 上需要重建表来加列，MySQL 上是直接 ALTER；
    # alembic 用 batch 模式两边都兼容
    with op.batch_alter_table('qa_sessions') as batch_op:
        batch_op.add_column(sa.Column('archived_at', sa.DateTime(), nullable=True))

    # 复合索引 (user_id, archived_at)：
    # 归档页查询 SELECT ... WHERE user_id = ? AND archived_at IS NOT NULL
    # 这两列联合索引能让上面这个查询走索引
    op.create_index(
        'idx_qa_sessions_user_archived',
        'qa_sessions',
        ['user_id', 'archived_at'],
    )


def downgrade() -> None:
    """回滚：先删索引，再删字段。"""
    op.drop_index('idx_qa_sessions_user_archived', table_name='qa_sessions')
    with op.batch_alter_table('qa_sessions') as batch_op:
        batch_op.drop_column('archived_at')
