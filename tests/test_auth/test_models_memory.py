"""验证 P1 记忆系统 2 张 ORM 表的 metadata 契约。

跟 test_models_homepage.py 一致：只断言 table_name / columns / PK / FK / index，
不起真 DB engine。
设计：[[记忆系统-设计]] §5（P1 仅 qa_user_memory + qa_session_memory）
"""
from src.service.db_models_homepage import QAUserMemory, QASessionMemory


# ───────── qa_user_memory ─────────

def test_user_memory_table_name():
    assert QAUserMemory.__tablename__ == "qa_user_memory"


def test_user_memory_columns():
    cols = {c.name for c in QAUserMemory.__table__.columns}
    assert cols == {
        "id", "user_id", "kind", "content", "source",
        "source_session_id", "status", "created_at", "updated_at",
    }


def test_user_memory_pk_is_id():
    cols = {c.name: c for c in QAUserMemory.__table__.columns}
    assert cols["id"].primary_key is True


def test_user_memory_no_user_fk():
    # 与 QASession.user_id 一致：故意不加 FK，保留已删用户的记忆。
    # 强断言『零 FK』（QAUserMemory 设计上无任何外键），避免 all() 空集合真空通过。
    fks = list(QAUserMemory.__table__.foreign_keys)
    assert fks == []


def test_user_memory_has_lookup_index():
    index_names = {idx.name for idx in QAUserMemory.__table__.indexes}
    assert "idx_qa_user_memory_user_active" in index_names


# ───────── qa_session_memory ─────────

def test_session_memory_table_name():
    assert QASessionMemory.__tablename__ == "qa_session_memory"


def test_session_memory_columns():
    cols = {c.name for c in QASessionMemory.__table__.columns}
    assert cols == {
        "id", "session_id", "working_summary",
        "focus_entity_ids", "turn_count", "updated_at",
    }


def test_session_memory_session_id_unique():
    cols = {c.name: c for c in QASessionMemory.__table__.columns}
    assert cols["session_id"].unique is True


def test_session_memory_has_session_cascade_fk():
    fks = list(QASessionMemory.__table__.foreign_keys)
    sess_fks = [fk for fk in fks if fk.column.table.name == "qa_sessions"]
    assert len(sess_fks) == 1
    assert sess_fks[0].ondelete == "CASCADE"


# ───────── qa_project_memory（工程级 S1，spec §19）─────────
from src.service.db_models_homepage import QAProjectMemory


def test_project_memory_table_name():
    assert QAProjectMemory.__tablename__ == "qa_project_memory"


def test_project_memory_columns():
    cols = {c.name for c in QAProjectMemory.__table__.columns}
    assert cols == {
        "id", "project_id", "user_id", "scope", "content",
        "entity_id", "entity_kind", "grounding_status", "source",
        "source_session_id", "confidence", "status",
        "promoted_by", "promoted_at", "vector_synced", "last_verified_at",
        "created_at", "updated_at",
    }


def test_project_memory_pk_is_id():
    cols = {c.name: c for c in QAProjectMemory.__table__.columns}
    assert cols["id"].primary_key is True


def test_project_memory_project_id_fk_cascade():
    fks = [fk for fk in QAProjectMemory.__table__.foreign_keys
           if fk.column.table.name == "projects"]
    assert len(fks) == 1
    assert fks[0].ondelete == "CASCADE"


def test_project_memory_user_id_no_fk():
    fks = [fk for fk in QAProjectMemory.__table__.foreign_keys
           if fk.column.table.name == "users"]
    assert fks == []


def test_project_memory_indexes():
    idx = {i.name for i in QAProjectMemory.__table__.indexes}
    assert {"idx_proj_scope", "idx_proj_user", "idx_entity"} <= idx
