"""ke_glob 工具测试 — Pathlib.glob + project-relative 返回 + head 截断。

设计：[[代码源文件查询工具-设计]] §3.2
"""
import asyncio

import pytest

from src.service.qa_engine.tools.ke_glob import build_ke_glob_tool


def _run(coro):
    return asyncio.run(coro)


def test_ke_glob_finds_files(tmp_path):
    """基本 glob 能列出匹配文件，path 是 project-relative。"""
    (tmp_path / "mall-admin").mkdir()
    (tmp_path / "mall-admin" / "FooMapper.xml").write_text("")
    (tmp_path / "mall-admin" / "BarController.java").write_text("")
    (tmp_path / "mall-portal").mkdir()
    (tmp_path / "mall-portal" / "BazMapper.xml").write_text("")

    tool = build_ke_glob_tool(str(tmp_path))
    result = _run(tool.handler({"pattern": "**/*Mapper.xml"}))

    assert "files" in result
    assert sorted(result["files"]) == sorted([
        "mall-admin/FooMapper.xml",
        "mall-portal/BazMapper.xml",
    ])


def test_ke_glob_head_truncates(tmp_path):
    """head 截断 + truncated=True。"""
    for i in range(10):
        (tmp_path / f"f{i}.txt").write_text("")

    tool = build_ke_glob_tool(str(tmp_path))
    result = _run(tool.handler({"pattern": "*.txt", "head": 3}))

    assert len(result["files"]) == 3
    assert result["truncated"] is True


def test_ke_glob_config_missing_returns_error():
    tool = build_ke_glob_tool(None)
    result = _run(tool.handler({"pattern": "*"}))
    assert "error" in result
    assert "not configured" in result["error"]


def test_ke_glob_missing_pattern_returns_error(tmp_path):
    tool = build_ke_glob_tool(str(tmp_path))
    result = _run(tool.handler({}))
    assert "error" in result
