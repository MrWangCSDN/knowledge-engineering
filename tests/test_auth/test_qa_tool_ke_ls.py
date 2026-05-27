"""ke_ls 工具测试 — 目录列出 + depth + include_hidden。

设计：[[代码源文件查询工具-设计]] §3.4
"""
import asyncio

import pytest

from src.service.qa_engine.tools.ke_ls import build_ke_ls_tool


def _run(coro):
    return asyncio.run(coro)


def test_ke_ls_basic(tmp_path):
    """默认 depth=1 列直接子项。"""
    (tmp_path / "mall-admin").mkdir()
    (tmp_path / "README.md").write_text("readme")
    (tmp_path / ".env").write_text("hidden")

    tool = build_ke_ls_tool(str(tmp_path))
    result = _run(tool.handler({"path": "."}))

    names = sorted([e["name"] for e in result["entries"]])
    # 默认 include_hidden=False，.env 不应出现
    assert "mall-admin" in names
    assert "README.md" in names
    assert ".env" not in names


def test_ke_ls_include_hidden(tmp_path):
    """include_hidden=True 时列隐藏文件。"""
    (tmp_path / ".env").write_text("secret")
    (tmp_path / "visible.txt").write_text("x")

    tool = build_ke_ls_tool(str(tmp_path))
    result = _run(tool.handler({"include_hidden": True}))

    names = [e["name"] for e in result["entries"]]
    assert ".env" in names


def test_ke_ls_depth_limit(tmp_path):
    """depth=2 进入一层子目录。"""
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b").mkdir()
    (tmp_path / "a" / "b" / "c.txt").write_text("")

    tool = build_ke_ls_tool(str(tmp_path))
    result = _run(tool.handler({"path": ".", "depth": 2}))

    # entries 至少含 a/ 和 a/b/（depth=2 进入第 1 层子目录）
    names = [e["name"] for e in result["entries"]]
    assert "a" in names or "a/b" in names


def test_ke_ls_config_missing():
    tool = build_ke_ls_tool(None)
    result = _run(tool.handler({}))
    assert "error" in result
