"""验证 User model 字段定义 + 默认值。"""
from src.service.auth_models import User


def test_user_table_name():
    assert User.__tablename__ == "users"


def test_user_columns():
    cols = {c.name for c in User.__table__.columns}
    # 2026-05-21 加 preferred_model 列（多模型切换功能 / users.preferred_model 持久化用户偏好）
    # P4a 加 github_user_id / gitlab_sub 列（GitHub/GitLab OAuth 接入）
    assert cols == {
        "id", "email", "username", "hashed_password",
        "is_active", "is_admin", "failed_attempts", "locked_until",
        "created_at", "updated_at",
        "preferred_model",
        "github_user_id", "gitlab_sub",
    }


def test_user_unique_constraints():
    cols_by_name = {c.name: c for c in User.__table__.columns}
    assert cols_by_name["email"].unique is True
    assert cols_by_name["username"].unique is True
