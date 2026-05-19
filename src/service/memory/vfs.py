"""文件式记忆 S1：命名空间 + 安全文件存储层（MemoryFS）。

设计：[[文件式记忆重构-设计]] §1。纯逻辑，不依赖 FastAPI。
URI 形如 ke://u/{user_id}/{rest}，物理映射到 <MEM_ROOT>/u/{user_id}/{rest}，
唯一隔离前缀 = <MEM_ROOT>/u/{user_id}/。仅 S1：不含 L0/L1/L2、召回、抽取、迁移。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

_log = logging.getLogger(__name__)


class MemoryPathError(Exception):
    """URI 非法 / 路径越界 / 跨租户 / 非目录等路径类错误。"""


class MemoryNotFound(Exception):
    """目标文件 / 目录不存在。"""


def mem_root() -> str:
    """记忆根目录。env KE_MEM_ROOT 优先；缺省 = 仓库根 /.ke-memory（绝对路径）。"""
    env = os.getenv("KE_MEM_ROOT")
    if env:
        return env
    repo_root = Path(__file__).resolve().parents[3]
    return str(repo_root / ".ke-memory")


class MemoryFS:  # noqa: D401  (Task 2 实现真正行为；此处仅占位让 import 成立)
    """占位：S1-T2 实现 resolve/read/write 等。"""
