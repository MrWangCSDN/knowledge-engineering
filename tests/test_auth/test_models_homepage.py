"""验证首页相关 ORM 模型定义（QAMessage/QAFeedback 已在 S6 删除）。

跟 test_models.py 一致，只断言 metadata 信息（table_name、columns、constraints），
不需要真起 DB engine。

S6 注：QAMessage / QAFeedback 已删（§7.1），对应测试函数一并删除。
"""
from src.service.db_models_homepage import (
    Project,
    GitCredential,
    UserProjectAccess,
    QASession,
)


# ───────── projects ─────────

def test_project_table_name():
    assert Project.__tablename__ == "projects"


def test_project_columns():
    cols = {c.name for c in Project.__table__.columns}
    assert cols == {
        # 基础字段
        "id", "name", "repo_url", "language",
        "status", "pipeline_at", "indexing_progress",
        "created_at", "created_by",
        # 仓库管理 v1.0 新增
        "git_url", "git_branch", "git_credential_id",
        "last_synced_at", "last_synced_commit", "sync_schedule",
        # v2.0 多租户新增
        "group_id",  # FK → groups.id，nullable=True，ondelete='SET NULL'
        # Task 1 源码文件工具新增：本地 clone 路径，供 ke_grep/ke_glob/ke_read_file/ke_ls 使用
        "repo_local_path",
        # GitHub 连接 P3 新增：SCM 绑定列（设计 §5.2）
        "scm_connection_id", "repo_external_id", "repo_full_name",
        "ref", "ref_type", "subpath",
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
        "created_at", "updated_at", "message_count", "archived_at",
        # 会话标题特性新增（[[会话标题-重命名与智能总结-设计]] §2）：
        # 是否被用户手动重命名过，异步总结据此决定是否覆盖
        "title_custom",
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


# ───────── git_credentials（v2.0 新增字段）─────────

def test_git_credential_has_owner_user_id():
    """v2.0 GitCredential 加 owner_user_id 字段（FK → users.id，nullable=True）。"""
    cols = {c.name for c in GitCredential.__table__.columns}
    assert "owner_user_id" in cols, f"GitCredential 缺少 owner_user_id 列，现有：{cols}"


def test_git_credential_owner_user_id_nullable():
    """owner_user_id 必须可为 NULL（迁移期间存量凭证尚未关联用户）。"""
    cols = {c.name: c for c in GitCredential.__table__.columns}
    assert cols["owner_user_id"].nullable is True


def test_git_credential_owner_user_id_fk_ondelete_set_null():
    """owner_user_id FK ondelete 应为 SET NULL（用户注销后凭证保留，配合审计）。"""
    fks = list(GitCredential.__table__.foreign_keys)
    user_fks = [fk for fk in fks if fk.column.table.name == "users"]
    assert len(user_fks) == 1, f"GitCredential 应有 1 个指向 users 的 FK，实际：{len(user_fks)}"
    assert user_fks[0].ondelete == "SET NULL"
