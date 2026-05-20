"""S6: 文件式记忆重构 — DB 残留清理（drop 5 stranded 表）

Revision ID: s6_drop_memory_tables
Revises: qa_project_memory_p2s1_v1
Create Date: 2026-05-22

设计：[[文件式记忆重构-设计]] §7.6。
S6 后：qa_user_memory / qa_project_memory / qa_session_memory / qa_messages /
qa_feedback 5 表 stranded（无 reader/writer），统一 drop。

QASession 保留（sessions 元数据仍在 DB；S6 不涉）。
Users / Projects / UserProjectAccess / RepoCredential 保留（业务表）。

测试数据被 alembic 抹去 — 接受（D3：迁移即清理；用户拍板：现存只是测试数据）。
"""
from alembic import op

# Alembic 版本标识
revision = "s6_drop_memory_tables"
down_revision = "qa_project_memory_p2s1_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 先 drop 子（qa_feedback FK → qa_messages），再 drop 父
    # qa_messages 和 qa_session_memory 都有 FK → qa_sessions ondelete=CASCADE，
    # 但 qa_sessions 不在 drop 列表，FK 由 alembic op.drop_table 自动 drop 即可
    op.drop_table("qa_feedback")
    op.drop_table("qa_messages")
    op.drop_table("qa_session_memory")
    op.drop_table("qa_user_memory")
    op.drop_table("qa_project_memory")


def downgrade() -> None:
    # 不支持 downgrade — 数据已被 drop（D3 单向接受）；
    # 测试数据已抹，downgrade 无意义；若真要还原 schema 仅作 schema 复活，
    # 需手工 op.create_table 加回（不复数据），非常规路径。
    raise NotImplementedError(
        "S6 不支持 downgrade — 数据已被 drop（设计 §7.6）；"
        "若需还原 schema 仅作 schema 复活（不复数据），请手工 op.create_table 加回。"
    )
