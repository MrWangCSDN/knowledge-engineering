"""路径沙箱单元测试 — resolve_safe_path 边界 + 越界 + symlink。

设计：[[代码源文件查询工具-设计]] §4
"""
from pathlib import Path

import pytest

from src.service.qa_engine.tools._path_sandbox import resolve_safe_path


def test_resolve_safe_path_normal_project_relative(tmp_path):
    """正常项目内 path → 返回拼接后的绝对路径。"""
    (tmp_path / "mall-admin").mkdir()
    (tmp_path / "mall-admin" / "pom.xml").write_text("<project/>")

    target = resolve_safe_path(str(tmp_path), "mall-admin/pom.xml")
    assert target == (tmp_path / "mall-admin" / "pom.xml").resolve()


def test_resolve_safe_path_dot_returns_repo_root(tmp_path):
    """'.' 视作项目根目录。"""
    target = resolve_safe_path(str(tmp_path), ".")
    assert target == tmp_path.resolve()


def test_resolve_safe_path_rejects_absolute_path(tmp_path):
    """绝对路径直接拒绝。"""
    with pytest.raises(ValueError, match="absolute"):
        resolve_safe_path(str(tmp_path), "/etc/passwd")


def test_resolve_safe_path_rejects_parent_escape(tmp_path):
    """../ 越界访问拒绝。"""
    with pytest.raises(ValueError, match="out of repo boundary"):
        resolve_safe_path(str(tmp_path), "../../../etc/passwd")


def test_resolve_safe_path_rejects_symlink_escape(tmp_path):
    """symlink 指向 repo 外 → 拒绝（realpath 会跟随）。"""
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret")
    (tmp_path / "linked").symlink_to(outside)

    with pytest.raises(ValueError, match="out of repo boundary"):
        resolve_safe_path(str(tmp_path), "linked")


def test_resolve_safe_path_rejects_empty_repo_path():
    """repo_local_path 为空 → 拒绝（NULL DB 字段场景）。"""
    with pytest.raises(ValueError, match="source path not configured"):
        resolve_safe_path("", "anything")

    with pytest.raises(ValueError, match="source path not configured"):
        resolve_safe_path(None, "anything")  # type: ignore[arg-type]
