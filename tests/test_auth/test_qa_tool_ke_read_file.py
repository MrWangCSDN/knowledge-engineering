"""ke_read_file 工具测试 — line range / 1MB 上限 / binary 拒 / sandbox 越界。

设计：[[代码源文件查询工具-设计]] §3.3
"""
import asyncio

import pytest

from src.service.qa_engine.tools.ke_read_file import build_ke_read_file_tool


def _run(coro):
    return asyncio.run(coro)


def test_ke_read_file_basic(tmp_path):
    """基本读 — 完整内容 + line range 字段。"""
    (tmp_path / "pom.xml").write_text("<project>\n  <name>x</name>\n</project>\n")

    tool = build_ke_read_file_tool(str(tmp_path))
    result = _run(tool.handler({"path": "pom.xml"}))

    assert result["path"] == "pom.xml"
    assert "<project>" in result["content"]
    assert result["line_start"] == 0
    assert result["eof"] is True
    assert result["total_lines"] == 3


def test_ke_read_file_offset_limit(tmp_path):
    """offset + limit 截断（line-based）。"""
    (tmp_path / "x.txt").write_text("\n".join(f"line{i}" for i in range(100)) + "\n")

    tool = build_ke_read_file_tool(str(tmp_path))
    result = _run(tool.handler({"path": "x.txt", "offset": 10, "limit": 5}))

    lines = result["content"].split("\n")
    assert lines[0] == "line10"
    assert lines[4] == "line14"
    assert result["line_start"] == 10
    assert result["line_end"] == 14
    assert result["eof"] is False


def test_ke_read_file_rejects_large_file(tmp_path):
    """文件 > 1MB → error。"""
    big = "x" * (1024 * 1024 + 100)
    (tmp_path / "big.txt").write_text(big)

    tool = build_ke_read_file_tool(str(tmp_path))
    result = _run(tool.handler({"path": "big.txt"}))

    assert "error" in result
    assert "too large" in result["error"].lower()


def test_ke_read_file_rejects_binary(tmp_path):
    """binary 文件拒读。"""
    (tmp_path / "bin").write_bytes(b"\x00\x01\x02\x03")

    tool = build_ke_read_file_tool(str(tmp_path))
    result = _run(tool.handler({"path": "bin"}))

    assert "error" in result
    assert "binary" in result["error"].lower()


def test_ke_read_file_path_traversal_rejected(tmp_path):
    """../etc/passwd 越界拒。"""
    tool = build_ke_read_file_tool(str(tmp_path))
    result = _run(tool.handler({"path": "../../../etc/passwd"}))

    assert "error" in result
    assert "boundary" in result["error"]


def test_ke_read_file_config_missing():
    tool = build_ke_read_file_tool(None)
    result = _run(tool.handler({"path": "x"}))
    assert "error" in result
    assert "not configured" in result["error"]
