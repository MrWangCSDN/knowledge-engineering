"""S1 文件式记忆存储层 MemoryFS 测试。设计：[[文件式记忆重构-设计]] §1。"""
import os
import asyncio
import pytest

from src.service.memory.vfs import (
    MemoryFS, MemoryPathError, MemoryNotFound, mem_root,
)


def test_exceptions_are_distinct_exception_types():
    assert issubclass(MemoryPathError, Exception)
    assert issubclass(MemoryNotFound, Exception)
    assert MemoryPathError is not MemoryNotFound


def test_mem_root_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("KE_MEM_ROOT", str(tmp_path / "mem"))
    assert mem_root() == str(tmp_path / "mem")


def test_mem_root_default_is_repo_dot_ke_memory(monkeypatch):
    monkeypatch.delenv("KE_MEM_ROOT", raising=False)
    root = mem_root()
    assert root.endswith("/.ke-memory")
    assert os.path.isabs(root)


def _fs(tmp_path):
    return MemoryFS(root=str(tmp_path))


def test_resolve_basic_maps_under_user_prefix(tmp_path):
    fs = _fs(tmp_path)
    p = fs.resolve("ke://u/7/global/profile.md")
    assert p == os.path.realpath(str(tmp_path / "u" / "7" / "global" / "profile.md"))


def test_resolve_user_root_itself_ok(tmp_path):
    fs = _fs(tmp_path)
    assert fs.resolve("ke://u/7") == os.path.realpath(str(tmp_path / "u" / "7"))
    assert fs.resolve("ke://u/7/") == os.path.realpath(str(tmp_path / "u" / "7"))


def test_resolve_leading_dot_filename_allowed(tmp_path):
    # S2 需要 .abstract.md/.overview.md —— 前导点文件名必须允许
    fs = _fs(tmp_path)
    p = fs.resolve("ke://u/7/project/deposit-system/.abstract.md")
    assert p.endswith("/u/7/project/deposit-system/.abstract.md")


@pytest.mark.parametrize("bad", [
    "http://u/7/x", "ke://x/7/a", "ke://u//a", "ke://u/7a/x",
    "ke://u/-1/x", "ke://u/7/../8/x", "ke://u/7/./x", "ke://u/7/a b/x",
    "ke://u/7/a\x00b", "ke://u/7/a/b/../../../etc", "ke:///u/7/x",
    "ke://u/7/项目/x", "ke://u/7/sub/..", "ke://u/", "ke://u",
])
def test_resolve_rejects_bad_uris(tmp_path, bad):
    fs = _fs(tmp_path)
    with pytest.raises(MemoryPathError):
        fs.resolve(bad)


def test_resolve_rejects_symlink_escape(tmp_path):
    fs = _fs(tmp_path)
    outside = tmp_path.parent / "outside_secret"
    outside.mkdir()
    udir = tmp_path / "u" / "7"
    udir.mkdir(parents=True)
    os.symlink(str(outside), str(udir / "leak"))   # u/7/leak -> 外部
    with pytest.raises(MemoryPathError):
        fs.resolve("ke://u/7/leak/secret.md")


def test_resolve_tenant_isolation(tmp_path):
    fs = _fs(tmp_path)
    p1 = fs.resolve("ke://u/1/global/a.md")
    p2 = fs.resolve("ke://u/2/global/a.md")
    assert "/u/1/" in p1 and "/u/2/" in p2 and p1 != p2
