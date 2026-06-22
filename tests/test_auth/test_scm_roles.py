# tests/test_auth/test_scm_roles.py
import pytest
from src.service.scm.base import ScmRole
from src.service.scm.scm_roles import github_role_to_scm, gitlab_access_level_to_scm


@pytest.mark.parametrize("role_name,expected", [
    ("admin", ScmRole.CAN_BIND), ("maintain", ScmRole.CAN_BIND),
    ("write", ScmRole.CAN_QUERY), ("triage", ScmRole.CAN_QUERY), ("read", ScmRole.CAN_QUERY),
    ("none", ScmRole.NOT_VISIBLE), ("", ScmRole.NOT_VISIBLE), (None, ScmRole.NOT_VISIBLE),
    ("ADMIN", ScmRole.CAN_BIND),  # 大小写不敏感
])
def test_github_role_name(role_name, expected):
    assert github_role_to_scm(role_name) == expected


def test_github_custom_role_falls_back_to_permission():
    # 自定义 org 角色（非内建）→ 回退 legacy permission 字段
    assert github_role_to_scm("custom-org-role", "read") == ScmRole.CAN_QUERY
    assert github_role_to_scm("custom-org-role", "admin") == ScmRole.CAN_BIND
    assert github_role_to_scm("custom-org-role", "none") == ScmRole.NOT_VISIBLE
    assert github_role_to_scm("custom-org-role", None) == ScmRole.NOT_VISIBLE


@pytest.mark.parametrize("level,expected", [
    (50, ScmRole.CAN_BIND), (40, ScmRole.CAN_BIND),
    (30, ScmRole.CAN_QUERY), (20, ScmRole.CAN_QUERY),
    (10, ScmRole.NOT_VISIBLE), (0, ScmRole.NOT_VISIBLE),  # Guest=10 → not_visible（Guest-trap）
])
def test_gitlab_access_level(level, expected):
    assert gitlab_access_level_to_scm(level) == expected
