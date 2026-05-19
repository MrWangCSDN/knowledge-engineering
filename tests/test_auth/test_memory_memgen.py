"""文件式记忆 S2：L0/L1 生成管线测试。设计：[[文件式记忆重构-设计]] §3。

fake/捕获 LLM + 真 MemoryFS(root=tmp_path)，沿用 tests/test_auth 既有风格。
"""
import pytest

from src.service.memory.vfs import MemoryFS
from src.service.memory.memgen import (
    MemoryGen,
    _sha256_hex,
    _split_frontmatter,
    _render_frontmatter,
)


def _fs(tmp_path):
    return MemoryFS(root=str(tmp_path))


# ── Task 1：纯函数 ───────────────────────────────────────────────
def test_sha256_hex_stable_and_utf8():
    # 同输入同输出（确定性）；中文按 UTF-8 编码
    assert _sha256_hex("用户的名字是李龙飞") == _sha256_hex("用户的名字是李龙飞")
    # 已知向量：空串 SHA-256
    assert _sha256_hex("") == (
        "e3b0c44298fc1c149afbf4c8996fb924"
        "27ae41e4649b934ca495991b7852b855"
    )
    # 不同输入→不同摘要
    assert _sha256_hex("a") != _sha256_hex("b")


def test_split_and_render_frontmatter_roundtrip():
    meta = {"src_hash": "deadbeef"}
    body = "用户的名字是李龙飞\n"
    text = _render_frontmatter(meta, body)
    assert text.startswith("---\n")
    got_meta, got_body = _split_frontmatter(text)
    assert got_meta == {"src_hash": "deadbeef"}
    assert got_body == body


def test_split_frontmatter_no_frontmatter_returns_empty_meta():
    # 不以 '---\n' 起 → ({}, 原文)
    meta, body = _split_frontmatter("just a body, no fm")
    assert meta == {}
    assert body == "just a body, no fm"


def test_split_frontmatter_unicode_preserved():
    # allow_unicode：中文不被转义成 \uXXXX
    text = _render_frontmatter({"k": "v"}, "中文正文\n")
    assert "中文正文" in text
    meta, body = _split_frontmatter(text)
    assert meta == {"k": "v"} and body == "中文正文\n"
