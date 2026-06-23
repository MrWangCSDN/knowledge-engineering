"""clone 命令构造（纯函数单测）+ gated 真实浅克隆。"""
import os
import shutil
import tempfile
import pytest

from src.service.scm.github_app import build_clone_args, mask_token


def test_build_clone_args_shallow_branch():
    args = build_clone_args(
        clone_url="https://x-access-token:ghs_abc@github.com/octocat/Hello-World.git",
        ref="master", dest="/tmp/dst",
    )
    assert args[:2] == ["git", "clone"]
    assert "--depth" in args and "1" in args
    assert "--branch" in args and "master" in args
    assert "--single-branch" in args
    assert args[-1] == "/tmp/dst"


def test_mask_token():
    txt = "fatal: auth https://x-access-token:ghs_SECRET@github.com/x.git"
    assert "ghs_SECRET" not in mask_token(txt, "ghs_SECRET")


@pytest.mark.asyncio
async def test_shallow_clone_cleans_existing_dest(monkeypatch, tmp_path):
    """当 dest 目录已存在（上次失败/重复索引残留）时，shallow_clone 应在 clone 前清空该目录。"""
    from src.service.scm.github_app import shallow_clone

    # 构造一个非空的残留 dest 目录（模拟上次失败后留下的垃圾文件）
    dest = str(tmp_path / "repo")
    os.makedirs(dest)                                 # 预先创建目录
    leftover = os.path.join(dest, "leftover.txt")
    open(leftover, "w").close()                       # 放入一个残留文件

    # mock _run：避免真正执行 git，只让它能正常返回；
    # 第一次调用（clone）时断言 leftover 已被清除，后续调用返回假 sha。
    call_count = 0

    async def fake_run(args, token, cwd=None):
        """模拟 _run：第一次调用时（即 clone 前）验证旧文件已消失。"""
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # 此时 shallow_clone 刚清完 dest、即将 clone
            # leftover 文件应已不存在
            assert not os.path.exists(leftover), "dest 内的残留文件应在 clone 前被清除"
        # rev-parse HEAD 调用返回假 sha，其他调用返回空字符串
        if "rev-parse" in args:
            return "a" * 40
        return ""

    # 用 monkeypatch 把 shallow_clone 所在模块的 _run 替换为 fake_run
    import src.service.scm.github_app as _mod
    monkeypatch.setattr(_mod, "_run", fake_run)

    sha = await shallow_clone("https://github.com/example/repo.git", ref="main", dest=dest, token=None)
    assert sha == "a" * 40    # 返回 rev-parse 的假 sha
    assert call_count >= 2    # 至少调用了 clone + rev-parse 两次


@pytest.mark.skipif(not os.getenv("KE_GATED_CLONE"), reason="gated：需联网；设 KE_GATED_CLONE=1 启用")
@pytest.mark.asyncio
async def test_real_shallow_clone_public_repo():
    from src.service.scm.github_app import shallow_clone
    dest = tempfile.mkdtemp(prefix="ke-clone-")
    try:
        sha = await shallow_clone(
            "https://github.com/octocat/Hello-World.git", ref="master", dest=dest, token=None
        )
        assert len(sha) == 40
        assert os.path.exists(os.path.join(dest, ".git"))
    finally:
        shutil.rmtree(dest, ignore_errors=True)
