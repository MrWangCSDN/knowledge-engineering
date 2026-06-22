"""SCM 抽象层数据类 + 枚举测试。"""
from src.service.scm.base import ScmRole, RepoInfo, BranchList, ScmIdentity


def test_scm_role_enum_values():
    assert {r.value for r in ScmRole} == {"can_bind", "can_query", "not_visible"}


def test_repo_info_fields():
    r = RepoInfo(external_id=42, full_name="macrozheng/mall-swarm", default_branch="master", private=True)
    assert r.external_id == 42
    assert r.full_name == "macrozheng/mall-swarm"
    assert r.private is True


def test_branch_list_default():
    bl = BranchList(default_branch="master", branches=["master", "dev"])
    assert bl.default_branch == "master"
    assert "dev" in bl.branches


def test_scm_identity():
    i = ScmIdentity(provider="github", scm_user_id="100", login="alice")
    assert i.provider == "github"
    assert i.scm_user_id == "100"
