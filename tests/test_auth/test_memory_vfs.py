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


@pytest.mark.parametrize("bad", [
    "ke://u/7//x", "ke://u/7/a//b", "", "ke://u/0/x", "ke://u/07/x",
])
def test_resolve_rejects_more_bad_uris(tmp_path, bad):
    fs = _fs(tmp_path)
    with pytest.raises(MemoryPathError):
        fs.resolve(bad)


def test_memoryfs_empty_root_rejected():
    with pytest.raises(ValueError):
        MemoryFS(root="")


def test_uid_of_and_resolve_share_parsing(tmp_path):
    fs = _fs(tmp_path)
    assert fs._uid_of("ke://u/7/global/a.md") == "7"
    # 同一非法输入,两条路径错误类型一致(共享解析)
    with pytest.raises(MemoryPathError):
        fs._uid_of("ke://u/0/x")
    with pytest.raises(MemoryPathError):
        fs.resolve("ke://u/0/x")


@pytest.mark.asyncio
async def test_write_then_read_roundtrip(tmp_path):
    fs = _fs(tmp_path)
    await fs.write("ke://u/7/global/a.md", "你好\nworld")
    assert await fs.read("ke://u/7/global/a.md") == "你好\nworld"


@pytest.mark.asyncio
async def test_write_creates_parent_dirs(tmp_path):
    fs = _fs(tmp_path)
    await fs.write("ke://u/7/project/deposit-system/notes/x.md", "c")
    assert os.path.isfile(
        os.path.join(str(tmp_path), "u", "7", "project",
                     "deposit-system", "notes", "x.md"))


@pytest.mark.asyncio
async def test_write_is_atomic_no_tmp_left(tmp_path):
    fs = _fs(tmp_path)
    await fs.write("ke://u/7/global/a.md", "v1")
    await fs.write("ke://u/7/global/a.md", "v2")
    d = os.path.join(str(tmp_path), "u", "7", "global")
    assert sorted(os.listdir(d)) == ["a.md"]          # 无 .tmp 残留
    assert await fs.read("ke://u/7/global/a.md") == "v2"


@pytest.mark.asyncio
async def test_read_missing_raises_not_found(tmp_path):
    fs = _fs(tmp_path)
    with pytest.raises(MemoryNotFound):
        await fs.read("ke://u/7/global/nope.md")


@pytest.mark.asyncio
async def test_read_on_dir_raises_path_error(tmp_path):
    fs = _fs(tmp_path)
    await fs.write("ke://u/7/global/a.md", "c")
    with pytest.raises(MemoryPathError):
        await fs.read("ke://u/7/global")


@pytest.mark.asyncio
async def test_exists(tmp_path):
    fs = _fs(tmp_path)
    assert await fs.exists("ke://u/7/global/a.md") is False
    await fs.write("ke://u/7/global/a.md", "c")
    assert await fs.exists("ke://u/7/global/a.md") is True
    assert await fs.exists("ke://u/7/global") is True   # 目录也算存在


@pytest.mark.asyncio
async def test_ls_three_states(tmp_path):
    fs = _fs(tmp_path)
    with pytest.raises(MemoryNotFound):
        await fs.ls("ke://u/7/global")                 # 不存在
    await fs.write("ke://u/7/global/a.md", "c")
    assert await fs.ls("ke://u/7/global") == ["a.md"]  # 有内容
    await fs.write("ke://u/7/global/b.md", "c")
    assert await fs.ls("ke://u/7/global") == ["a.md", "b.md"]  # 排序
    with pytest.raises(MemoryPathError):
        await fs.ls("ke://u/7/global/a.md")            # 非目录


@pytest.mark.asyncio
async def test_rm_file_and_missing(tmp_path):
    fs = _fs(tmp_path)
    await fs.write("ke://u/7/global/a.md", "c")
    await fs.rm("ke://u/7/global/a.md")
    assert await fs.exists("ke://u/7/global/a.md") is False
    with pytest.raises(MemoryNotFound):
        await fs.rm("ke://u/7/global/a.md")


@pytest.mark.asyncio
async def test_rm_dir_needs_recursive(tmp_path):
    fs = _fs(tmp_path)
    await fs.write("ke://u/7/project/p1/x.md", "c")
    with pytest.raises(MemoryPathError):
        await fs.rm("ke://u/7/project/p1")             # 目录但未 recursive
    await fs.rm("ke://u/7/project/p1", recursive=True)
    assert await fs.exists("ke://u/7/project/p1") is False


@pytest.mark.asyncio
async def test_mv_within_user(tmp_path):
    fs = _fs(tmp_path)
    await fs.write("ke://u/7/global/a.md", "hello")
    await fs.mv("ke://u/7/global/a.md", "ke://u/7/project/p1/a.md")
    assert await fs.exists("ke://u/7/global/a.md") is False
    assert await fs.read("ke://u/7/project/p1/a.md") == "hello"


@pytest.mark.asyncio
async def test_mv_cross_user_rejected(tmp_path):
    fs = _fs(tmp_path)
    await fs.write("ke://u/7/global/a.md", "hello")
    with pytest.raises(MemoryPathError):
        await fs.mv("ke://u/7/global/a.md", "ke://u/8/global/a.md")
    assert await fs.exists("ke://u/7/global/a.md") is True  # 源未动


@pytest.mark.asyncio
async def test_mv_missing_src_not_found(tmp_path):
    fs = _fs(tmp_path)
    with pytest.raises(MemoryNotFound):
        await fs.mv("ke://u/7/global/nope.md", "ke://u/7/global/b.md")


@pytest.mark.asyncio
async def test_mv_dst_exists_file_rejected(tmp_path):
    fs = _fs(tmp_path)
    await fs.write("ke://u/7/global/a.md", "A")
    await fs.write("ke://u/7/global/b.md", "B")
    with pytest.raises(MemoryPathError):
        await fs.mv("ke://u/7/global/a.md", "ke://u/7/global/b.md")
    assert await fs.read("ke://u/7/global/a.md") == "A"   # 源未动
    assert await fs.read("ke://u/7/global/b.md") == "B"   # dst 未被覆盖


@pytest.mark.asyncio
async def test_mv_dst_exists_dir_rejected(tmp_path):
    fs = _fs(tmp_path)
    await fs.write("ke://u/7/global/a.md", "A")
    await fs.write("ke://u/7/project/p1/x.md", "X")       # 使 project/p1 成已存在目录
    with pytest.raises(MemoryPathError):
        await fs.mv("ke://u/7/global/a.md", "ke://u/7/project/p1")
    assert await fs.read("ke://u/7/global/a.md") == "A"   # 不静默并入目录


@pytest.mark.asyncio
async def test_rm_empty_dir_still_needs_recursive(tmp_path):
    fs = _fs(tmp_path)
    await fs.write("ke://u/7/project/p1/x.md", "c")
    await fs.rm("ke://u/7/project/p1/x.md")               # 删文件 → p1 空目录
    with pytest.raises(MemoryPathError):
        await fs.rm("ke://u/7/project/p1")                # 空目录仍需 recursive
    await fs.rm("ke://u/7/project/p1", recursive=True)
    assert await fs.exists("ke://u/7/project/p1") is False


@pytest.mark.asyncio
async def test_ls_empty_dir_returns_empty_list(tmp_path):
    fs = _fs(tmp_path)
    await fs.write("ke://u/7/global/a.md", "c")
    await fs.rm("ke://u/7/global/a.md")                   # global 变空目录
    assert await fs.ls("ke://u/7/global") == []
