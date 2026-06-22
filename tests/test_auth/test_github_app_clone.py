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
