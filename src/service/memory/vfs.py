"""文件式记忆 S1：命名空间 + 安全文件存储层（MemoryFS）。

设计：[[文件式记忆重构-设计]] §1。纯逻辑，不依赖 FastAPI。
URI 形如 ke://u/{user_id}/{rest}，物理映射到 <MEM_ROOT>/u/{user_id}/{rest}，
唯一隔离前缀 = <MEM_ROOT>/u/{user_id}/。仅 S1：不含 L0/L1/L2、召回、抽取、迁移。
"""
from __future__ import annotations

import asyncio
import contextlib
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
# user_id 为规范化正整数（KE DB Integer 自增、从 1 起）；拒 0 与前导零，
# 避免 "07" 与 "7" 解析到不同物理目录的租户一致性隐患
_UID_RE = re.compile(r"[1-9][0-9]*")
_URI_PREFIX = "ke://u/"


class MemoryFS:
    """ke://u/{user_id}/... 安全文件存储层（S1）。

    root 不传则用 mem_root()。所有方法 async（配 per-path asyncio.Lock）；
    文件 IO 同步 stdlib（单实例、post-turn 小文件，YAGNI 不引新依赖）。
    """

    def __init__(self, root: str | None = None) -> None:
        if root is not None and not root:
            raise ValueError("root must be non-empty if provided")
        self._root = os.path.realpath(root if root is not None else mem_root())
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _parse_uri(uri: str) -> tuple[str, list[str]]:
        """解析 ke://u/{uid}/seg... → (uid, 原始段列表)；非法抛 MemoryPathError。

        resolve 与 _uid_of 共用此解析，杜绝两处 URI 解析分歧
        （分歧会让"路由到的 uid"与"跨租户校验的 uid"不一致 = 安全缺陷）。
        """
        if not isinstance(uri, str) or not uri.startswith(_URI_PREFIX):
            raise MemoryPathError(f"bad uri: {uri!r}")
        parts = uri[len("ke://"):].split("/")            # ["u","{uid}", ...segs]
        if len(parts) < 2 or parts[0] != "u" or not _UID_RE.fullmatch(parts[1]):
            raise MemoryPathError(f"bad uri: {uri!r}")
        return parts[1], parts[2:]

    def _user_base(self, user_id: str) -> str:
        return os.path.realpath(os.path.join(self._root, "u", user_id))

    def resolve(self, uri: str) -> str:
        """ke://u/{uid}/{seg/...} → 绝对物理路径；非法/越界抛 MemoryPathError。"""
        uid, segs = self._parse_uri(uri)
        # 末尾 "/" 产生空段；其余位置空段非法
        if segs and segs[-1] == "":
            segs = segs[:-1]
        for s in segs:
            # 仅精确 "." / ".." 是穿越段；"..." 等是合法 POSIX 文件名
            # （realpath 不当穿越处理），故只精确等值拒；穿越最终防线是
            # 下方 realpath 前缀断言
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
        """取 uri 的 user_id 段（供跨租户校验）；非法抛 MemoryPathError。"""
        uid, _ = MemoryFS._parse_uri(uri)
        return uid

    def _lock_for(self, path: str) -> asyncio.Lock:
        """取/建该物理路径的进程内锁（write/rm/mv 串行化）。

        注：字典随不同路径增长、不淘汰；单实例 + 有界用户量下可接受
        （跨实例锁见 NAS 阶段，非 S1 范围）。
        """
        lk = self._locks.get(path)
        if lk is None:
            lk = asyncio.Lock()
            self._locks[path] = lk
        return lk

    async def write(self, uri: str, content: str) -> None:
        """原子写：同目录 tmp + os.replace；自动 mkdir -p 父目录。

        刻意不做 fsync(文件/父目录)：os.replace 保证并发读者只见旧或新完整内容
        (内容崩溃一致),但不保证掉电持久。记忆是辅助层(设计 §4.3,失败不影响主答、
        下一轮可重新派生),为此免去每写一次 fsync 的延迟;持久性留 NAS/后续阶段。
        """
        path = self.resolve(uri)
        parent = os.path.dirname(path)
        async with self._lock_for(path):
            os.makedirs(parent, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                os.replace(tmp, path)                  # POSIX 原子 rename(同目录同 fs)
            except BaseException:
                # 清理半成品 tmp 后透传(含 KeyboardInterrupt/SystemExit)；
                # replace 成功后 tmp 已不存在 → 内层 OSError 兜住 FileNotFound
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    async def read(self, uri: str) -> str:
        path = self.resolve(uri)
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            raise MemoryNotFound(uri)
        except IsADirectoryError:
            raise MemoryPathError(f"not a file: {uri!r}")

    async def exists(self, uri: str) -> bool:
        return os.path.exists(self.resolve(uri))

    async def ls(self, uri: str) -> list[str]:
        """列目录条目（已排序）。不存在→MemoryNotFound；非目录→MemoryPathError；空→[]。

        无锁读：与并发 rm 交错时 os.isdir/listdir 可能竞态抛裸 FileNotFoundError，
        统一映射为 MemoryNotFound，守住"只抛 MemoryNotFound/MemoryPathError"公开契约。
        """
        path = self.resolve(uri)
        try:
            if not os.path.isdir(path):
                if not os.path.exists(path):
                    raise MemoryNotFound(uri)
                raise MemoryPathError(f"not a directory: {uri!r}")
            return sorted(os.listdir(path))
        except FileNotFoundError:
            raise MemoryNotFound(uri)

    async def rm(self, uri: str, *, recursive: bool = False) -> None:
        """删文件/子树（仅 FS，不涉索引——索引联动是 S3）。

        不存在→MemoryNotFound；目录须 recursive=True（即使空目录亦然）否则
        MemoryPathError；文件 os.remove、目录 shutil.rmtree。per-path 锁内、
        check→op 间无 await，故对同 path 原子。
        """
        path = self.resolve(uri)
        async with self._lock_for(path):
            if not os.path.exists(path):
                raise MemoryNotFound(uri)
            if os.path.isdir(path):
                if not recursive:
                    raise MemoryPathError(
                        f"is a directory (need recursive=True): {uri!r}")
                shutil.rmtree(path)
            else:
                os.remove(path)

    async def mv(self, src_uri: str, dst_uri: str) -> None:
        """同 user 内移动（跨 user 前缀→MemoryPathError，IO 前即拒、源不动）。

        dst 已存在→MemoryPathError（不静默覆盖文件/不并入目录，行为可预期）；
        源不存在→MemoryNotFound；同 fs 用 os.replace 原子 rename（无 copytree
        回退）。src/dst 双锁、按 path 排序去重获取（防与反向 mv 死锁、防与
        write(dst)/rm(dst) 跨协程竞态；src==dst 时只取一把不自死锁）。
        """
        if self._uid_of(src_uri) != self._uid_of(dst_uri):
            raise MemoryPathError(
                f"cross-user mv forbidden: {src_uri!r} -> {dst_uri!r}")
        src = self.resolve(src_uri)
        dst = self.resolve(dst_uri)
        async with contextlib.AsyncExitStack() as stack:
            for p in sorted({src, dst}):          # set 去重(src==dst)、sorted 全局序防死锁
                await stack.enter_async_context(self._lock_for(p))
            if not os.path.exists(src):
                raise MemoryNotFound(src_uri)
            if os.path.exists(dst):
                raise MemoryPathError(f"mv destination exists: {dst_uri!r}")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.replace(src, dst)               # 同 <root>/u/{uid} 同 fs → 原子 rename
