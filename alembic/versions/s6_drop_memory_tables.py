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
    """S6 downgrade：重建 5 张被 upgrade 删掉的表（仅 schema，不复数据）。

    D3 单向接受（测试数据已抹）。downgrade 仅恢复 schema，使得
    后续 migration chain（qa_project_memory_p2s1_v1 → qa_memory_p1_v1 → ...）
    各自的 downgrade 能正常执行（它们会 drop_table / drop_index 这些表）。

    不提供数据恢复 — upgrade 已不可逆地删除了所有行；若需恢复数据，
    请手工从备份还原。
    """
    import sqlalchemy as sa

    # 重建顺序：先建父表，再建有 FK 依赖的子表
    # 1. qa_messages（FK → qa_sessions）
    op.create_table(
        "qa_messages",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("sections", sa.JSON(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["qa_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_qa_messages_session", "qa_messages", ["session_id", "created_at"])

    # 2. qa_feedback（FK → qa_messages）
    op.create_table(
        "qa_feedback",
        sa.Column("message_id", sa.String(64), nullable=False),
        sa.Column("vote", sa.String(8), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["qa_messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("message_id"),
    )

    # 3. qa_session_memory（FK → qa_sessions）
    op.create_table(
        "qa_session_memory",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.String(64), nullable=False, unique=True),
        sa.Column("working_summary", sa.Text(), nullable=False),
        sa.Column("focus_entity_ids", sa.JSON(), nullable=True),
        sa.Column("turn_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["qa_sessions.id"], ondelete="CASCADE"),
    )

    # 4. qa_user_memory（无 FK 约束）
    op.create_table(
        "qa_user_memory",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False, server_default=sa.text("'explicit'")),
        sa.Column("source_session_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'active'")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_qa_user_memory_user_active", "qa_user_memory", ["user_id", "status"])

    # 5. qa_project_memory（FK → projects）
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
