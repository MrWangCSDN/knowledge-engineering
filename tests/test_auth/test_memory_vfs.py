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
