"""验证首页相关 ORM 模型定义（5 张表）。

跟 test_models.py 一致，只断言 metadata 信息（table_name、columns、constraints），
不需要真起 DB engine。
"""
from src.service.db_models_homepage import (
    Project,
    UserProjectAccess,
    QASession,
    QAMessage,
    QAFeedback,
)


# ───────── projects ─────────

def test_project_table_name():
    assert Project.__tablename__ == "projects"


def test_project_columns():
    cols = {c.name for c in Project.__table__.columns}
    assert cols == {
        "id", "name", "repo_url", "language",
        "status", "pipeline_at", "indexing_progress",
        "created_at", "created_by",
    }


def test_project_id_is_primary_key():
    cols = {c.name: c for c in Project.__table__.columns}
    assert cols["id"].primary_key is True


def test_project_has_status_index():
    """plan 要求按 status 查询时走索引。"""
    index_names = {idx.name for idx in Project.__table__.indexes}
    assert "idx_projects_status" in index_names


# ───────── user_project_access ─────────

def test_upa_table_name():
    assert UserProjectAccess.__tablename__ == "user_project_access"


def test_upa_composite_primary_key():
    pk_cols = {c.name for c in UserProjectAccess.__table__.primary_key.columns}
    assert pk_cols == {"user_id", "project_id"}


def test_upa_has_project_fk():
    fks = list(UserProjectAccess.__table__.foreign_keys)
    fk_targets = {fk.column.table.name for fk in fks}
    assert "projects" in fk_targets


# ───────── qa_sessions ─────────

def test_qa_session_table_name():
    assert QASession.__tablename__ == "qa_sessions"


def test_qa_session_columns():
    cols = {c.name for c in QASession.__table__.columns}
    assert cols == {
        "id", "project_id", "user_id", "title",
        "created_at", "updated_at", "message_count",
    }


def test_qa_session_has_project_cascade_fk():
    """删除工程时级联删它的会话。"""
    fks = list(QASession.__table__.foreign_keys)
    project_fks = [fk for fk in fks if fk.column.table.name == "projects"]
    assert len(project_fks) == 1
    assert project_fks[0].ondelete == "CASCADE"


def test_qa_session_has_lookup_index():
    """plan 要求 (project_id, user_id, updated_at DESC) 这条复合索引。"""
    index_names = {idx.name for idx in QASession.__table__.indexes}
    assert "idx_qa_sessions_project_user" in index_names


# ───────── qa_messages ─────────

def test_qa_message_table_name():
    assert QAMessage.__tablename__ == "qa_messages"


def test_qa_message_columns():
    """metadata 列名注意：Python 属性叫 msg_metadata，DB 列名是 metadata。
    (SQLAlchemy DeclarativeBase 自带 metadata 属性，会冲突，所以用 mapped_column('metadata') 重命名。)"""
    cols = {c.name for c in QAMessage.__table__.columns}
    assert cols == {
        "id", "session_id", "role", "content",
        "sections", "metadata", "created_at",
    }


def test_qa_message_has_session_cascade_fk():
    fks = list(QAMessage.__table__.foreign_keys)
    sess_fks = [fk for fk in fks if fk.column.table.name == "qa_sessions"]
    assert len(sess_fks) == 1
    assert sess_fks[0].ondelete == "CASCADE"


# ───────── qa_feedback ─────────

def test_qa_feedback_table_name():
    assert QAFeedback.__tablename__ == "qa_feedback"


def test_qa_feedback_columns():
    cols = {c.name for c in QAFeedback.__table__.columns}
    assert cols == {"message_id", "vote", "comment", "user_id", "created_at"}


def test_qa_feedback_message_id_pk_and_cascade():
    cols = {c.name: c for c in QAFeedback.__table__.columns}
    assert cols["message_id"].primary_key is True
    fks = list(QAFeedback.__table__.foreign_keys)
    msg_fks = [fk for fk in fks if fk.column.table.name == "qa_messages"]
    assert len(msg_fks) == 1
    assert msg_fks[0].ondelete == "CASCADE"
