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


# 单段合法字符：字母数字 . _ -（含前导点文件名如 .abstract.md）
_SEG_RE = re.compile(r"[A-Za-z0-9._-]+")
_UID_RE = re.compile(r"[0-9]+")
_URI_PREFIX = "ke://u/"


class MemoryFS:
    """ke://u/{user_id}/... 安全文件存储层（S1）。

    root 不传则用 mem_root()。所有方法 async（配 per-path asyncio.Lock）；
    文件 IO 同步 stdlib（单实例、post-turn 小文件，YAGNI 不引新依赖）。
    """

    def __init__(self, root: str | None = None) -> None:
        self._root = os.path.realpath(root if root is not None else mem_root())
        self._locks: dict[str, asyncio.Lock] = {}

    def _user_base(self, user_id: str) -> str:
        return os.path.realpath(os.path.join(self._root, "u", user_id))

    def resolve(self, uri: str) -> str:
        """ke://u/{uid}/{seg/...} → 绝对物理路径；非法/越界抛 MemoryPathError。"""
        if not isinstance(uri, str) or not uri.startswith(_URI_PREFIX):
            raise MemoryPathError(f"bad uri scheme: {uri!r}")
        rest = uri[len("ke://"):]                       # "u/{uid}/...."
        parts = rest.split("/")                          # ["u","{uid}", ...segs]
        if len(parts) < 2 or parts[0] != "u":
            raise MemoryPathError(f"bad uri: {uri!r}")
        uid = parts[1]
        if not _UID_RE.fullmatch(uid):
            raise MemoryPathError(f"bad user_id: {uid!r}")
        # 末尾 "/" 产生空段；其余位置空段非法
        segs = parts[2:]
        if segs and segs[-1] == "":
            segs = segs[:-1]
        for s in segs:
            if (s == "" or s in (".", "..") or "\x00" in s
                    or not _SEG_RE.fullmatch(s)):
                raise MemoryPathError(f"bad segment {s!r} in {uri!r}")
        base = self._user_base(uid)
        target = os.path.realpath(os.path.join(base, *segs)) if segs else base
        if not (target == base or target.startswith(base + os.sep)):
            raise MemoryPathError(f"path escapes tenant prefix: {uri!r}")
        return target

    @staticmethod
    def _uid_of(uri: str) -> str:
        """取 uri 的 user_id 段（供跨租户校验）；非法 uri 抛 MemoryPathError。"""
        if not isinstance(uri, str) or not uri.startswith(_URI_PREFIX):
            raise MemoryPathError(f"bad uri: {uri!r}")
        parts = uri[len("ke://"):].split("/")
        if len(parts) < 2 or parts[0] != "u" or not _UID_RE.fullmatch(parts[1]):
            raise MemoryPathError(f"bad uri: {uri!r}")
        return parts[1]

    def _lock_for(self, path: str) -> asyncio.Lock:
        lk = self._locks.get(path)
        if lk is None:
            lk = asyncio.Lock()
            self._locks[path] = lk
        return lk
