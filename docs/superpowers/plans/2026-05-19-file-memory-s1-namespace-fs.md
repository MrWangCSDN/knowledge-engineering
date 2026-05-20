# 文件式记忆重构 — S1 命名空间+文件存储层 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建一个安全、可寻址、单实例并发安全的文件存储层 `MemoryFS`（`ke://u/{user_id}/...` 寻址 + 路径前缀隔离 + 原子写），作为文件式记忆重构的地基。

**Architecture:** 纯逻辑模块 `src/service/memory/vfs.py`（不依赖 FastAPI，沿用 service.py 风格）。`MemoryFS` 把 `ke://u/{user_id}/{rest}` 解析为 `<MEM_ROOT>/u/{user_id}/{rest}` 物理路径，每段白名单校验 + realpath 前缀断言防穿越/符号链接逃逸；方法 `async def`（配 per-path `asyncio.Lock`）但文件 IO 用同步 stdlib（单实例、post-turn 小文件，YAGNI，不引新依赖）。

**Tech Stack:** Python 3，stdlib `os`/`re`/`shutil`/`tempfile`/`asyncio`/`pathlib`；pytest + `@pytest.mark.asyncio` + `tmp_path`。

**Spec（单一来源）：** `/Users/java/obsidian/01 Engineering/knowledge-engineering/文件式记忆重构-设计.md` §1（S1）+ §0 锁定决策。**仅 S1**；不做 L0/L1/L2(S2)/Weaviate 召回(S3)/抽取提交(S4)/会话改造(S5)/迁移(S6)/跨实例锁(NAS)。

**Repo:** `/Users/java/knowledge-engineering-auth`，分支 `release-0513`，逐任务提交（已授权）。

---

## 现状基线（已核对真实代码 2026-05-19）

- `src/service/memory/`：`__init__.py`（仅 docstring，无导出）、`context_budget.py`、`service.py`。KE 直接 `from src.service.memory.service import ...`，**不经包 `__init__` 导出** → 新增 `vfs.py` 无需改 `__init__.py`。
- `service.py` 风格：`from __future__ import annotations` → stdlib（`json,logging,re`）→ `from typing import Any` → `_log = logging.getLogger(__name__)`。纯逻辑、不 import FastAPI。新模块照此。
- env 惯例：`os.getenv("KE_X", "default")`（见 `db.py:20`、`auth_security.py:40-58`）。`.env.local` 在 `src/service/api.py:34` 由 `load_dotenv` 加载；纯逻辑模块只 `os.getenv` 即可。
- pytest：`pyproject.toml [tool.pytest.ini_options] testpaths=["tests"]`，无 `asyncio_mode` → 异步测试须 `@pytest.mark.asyncio` 装饰（本会话 66+ 测试均此法，已验证）。S1 测试用标准 `tmp_path` fixture 当 MEM_ROOT，无需 DB/LLM fake。
- 仓库根 = `Path(__file__).resolve().parents[3]`（`vfs.py`→memory→service→src→repo）。
- 跑测试：`cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest ...`。

## File Structure

| 文件 | 职责 |
|---|---|
| `src/service/memory/vfs.py` | **新建**：`MemoryPathError`/`MemoryNotFound` 异常、`mem_root()` 配置、`MemoryFS` 类（resolve/read/write/exists/ls/rm/mv + per-path 锁） |
| `tests/test_auth/test_memory_vfs.py` | **新建**：S1 全部行为测试（pytest + tmp_path + asyncio） |

S1 纯新增，不改任何既有文件。

---

## Task 1：异常 + 配置 + 模块骨架

**Files:** Create `src/service/memory/vfs.py`; Test `tests/test_auth/test_memory_vfs.py`

- [ ] **Step 1：写失败测试** —— 新建 `tests/test_auth/test_memory_vfs.py`：

```python
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
```

- [ ] **Step 2：跑，确认失败** —— Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_vfs.py -q`
  Expected: FAIL —— `ModuleNotFoundError: No module named 'src.service.memory.vfs'`。

- [ ] **Step 3：实现** —— 新建 `src/service/memory/vfs.py`：

```python
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
```

- [ ] **Step 4：跑，确认通过** —— Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_vfs.py -q`
  Expected: 3 passed。

- [ ] **Step 5：Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/memory/vfs.py tests/test_auth/test_memory_vfs.py
git commit -m "$(cat <<'EOF'
feat(memory): S1 文件式记忆存储层骨架——异常 + KE_MEM_ROOT 配置（TDD）

新增 src/service/memory/vfs.py：MemoryPathError/MemoryNotFound + mem_root()
（KE_MEM_ROOT env / 缺省仓库根 .ke-memory）。设计 [[文件式记忆重构-设计]] §1。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2：`MemoryFS.resolve` —— URI 解析 + 段白名单 + realpath 前缀断言

**Files:** Modify `src/service/memory/vfs.py`; Test `tests/test_auth/test_memory_vfs.py`

- [ ] **Step 1：写失败测试** —— 追加到 `tests/test_auth/test_memory_vfs.py` 末尾：

```python
def _fs(tmp_path):
    return MemoryFS(root=str(tmp_path))


def test_resolve_basic_maps_under_user_prefix(tmp_path):
    fs = _fs(tmp_path)
    p = fs.resolve("ke://u/7/global/profile.md")
    assert p == os.path.realpath(str(tmp_path / "u" / "7" / "global" / "profile.md"))


def test_resolve_user_root_itself_ok(tmp_path):
    fs = _fs(tmp_path)
    assert fs.resolve("ke://u/7") == os.path.realpath(str(tmp_path / "u" / "7"))
    assert fs.resolve("ke://u/7/") == os.path.realpath(str(tmp_path / "u" / "7"))


def test_resolve_leading_dot_filename_allowed(tmp_path):
    # S2 需要 .abstract.md/.overview.md —— 前导点文件名必须允许
    fs = _fs(tmp_path)
    p = fs.resolve("ke://u/7/project/deposit-system/.abstract.md")
    assert p.endswith("/u/7/project/deposit-system/.abstract.md")


@pytest.mark.parametrize("bad", [
    "http://u/7/x", "ke://x/7/a", "ke://u//a", "ke://u/7a/x",
    "ke://u/-1/x", "ke://u/7/../8/x", "ke://u/7/./x", "ke://u/7/a b/x",
    "ke://u/7/a\x00b", "ke://u/7/a/b/../../../etc", "ke:///u/7/x",
    "ke://u/7/项目/x", "ke://u/7/sub/..", "ke://u/", "ke://u",
])
def test_resolve_rejects_bad_uris(tmp_path, bad):
    fs = _fs(tmp_path)
    with pytest.raises(MemoryPathError):
        fs.resolve(bad)


def test_resolve_rejects_symlink_escape(tmp_path):
    fs = _fs(tmp_path)
    outside = tmp_path.parent / "outside_secret"
    outside.mkdir()
    udir = tmp_path / "u" / "7"
    udir.mkdir(parents=True)
    os.symlink(str(outside), str(udir / "leak"))   # u/7/leak -> 外部
    with pytest.raises(MemoryPathError):
        fs.resolve("ke://u/7/leak/secret.md")


def test_resolve_tenant_isolation(tmp_path):
    fs = _fs(tmp_path)
    p1 = fs.resolve("ke://u/1/global/a.md")
    p2 = fs.resolve("ke://u/2/global/a.md")
    assert "/u/1/" in p1 and "/u/2/" in p2 and p1 != p2
```

- [ ] **Step 2：跑，确认失败** —— Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_vfs.py -q -k resolve`
  Expected: FAIL —— `AttributeError`/`TypeError`（`MemoryFS` 尚无 `__init__`/`resolve`）。

- [ ] **Step 3：实现** —— 在 `src/service/memory/vfs.py` 末尾追加（`mem_root` 之后）：

```python
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
```

- [ ] **Step 4：跑，确认通过** —— Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_vfs.py -q`
  Expected: 全 passed（含 Task1 的 3 个 + resolve 全部参数化用例 + 符号链接逃逸 + 租户隔离）。

- [ ] **Step 5：Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/memory/vfs.py tests/test_auth/test_memory_vfs.py
git commit -m "$(cat <<'EOF'
feat(memory): S1 MemoryFS.resolve——URI 解析+段白名单+realpath 前缀断言（TDD）

ke://u/{uid}/seg... → <root>/u/{uid}/...；段须全匹配 [A-Za-z0-9._-]+ 且非
. / ..，前导点文件名允许；realpath 后断言仍在租户前缀内（防 ../ 与符号链接
逃逸）；租户隔离。设计 §1.2。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3：`write`（原子）+ `read` + `exists`

**Files:** Modify `src/service/memory/vfs.py`; Test `tests/test_auth/test_memory_vfs.py`

- [ ] **Step 1：写失败测试** —— 追加到 `tests/test_auth/test_memory_vfs.py` 末尾：

```python
@pytest.mark.asyncio
async def test_write_then_read_roundtrip(tmp_path):
    fs = _fs(tmp_path)
    await fs.write("ke://u/7/global/a.md", "你好\nworld")
    assert await fs.read("ke://u/7/global/a.md") == "你好\nworld"


@pytest.mark.asyncio
async def test_write_creates_parent_dirs(tmp_path):
    fs = _fs(tmp_path)
    await fs.write("ke://u/7/project/deposit-system/notes/x.md", "c")
    assert os.path.isfile(
        os.path.join(str(tmp_path), "u", "7", "project",
                     "deposit-system", "notes", "x.md"))


@pytest.mark.asyncio
async def test_write_is_atomic_no_tmp_left(tmp_path):
    fs = _fs(tmp_path)
    await fs.write("ke://u/7/global/a.md", "v1")
    await fs.write("ke://u/7/global/a.md", "v2")
    d = os.path.join(str(tmp_path), "u", "7", "global")
    assert sorted(os.listdir(d)) == ["a.md"]          # 无 .tmp 残留
    assert await fs.read("ke://u/7/global/a.md") == "v2"


@pytest.mark.asyncio
async def test_read_missing_raises_not_found(tmp_path):
    fs = _fs(tmp_path)
    with pytest.raises(MemoryNotFound):
        await fs.read("ke://u/7/global/nope.md")


@pytest.mark.asyncio
async def test_read_on_dir_raises_path_error(tmp_path):
    fs = _fs(tmp_path)
    await fs.write("ke://u/7/global/a.md", "c")
    with pytest.raises(MemoryPathError):
        await fs.read("ke://u/7/global")


@pytest.mark.asyncio
async def test_exists(tmp_path):
    fs = _fs(tmp_path)
    assert await fs.exists("ke://u/7/global/a.md") is False
    await fs.write("ke://u/7/global/a.md", "c")
    assert await fs.exists("ke://u/7/global/a.md") is True
    assert await fs.exists("ke://u/7/global") is True   # 目录也算存在
```

- [ ] **Step 2：跑，确认失败** —— Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_vfs.py -q -k "write or read or exists"`
  Expected: FAIL —— `AttributeError: 'MemoryFS' object has no attribute 'write'`。

- [ ] **Step 3：实现** —— 在 `MemoryFS` 类内（`_lock_for` 之后）追加方法：

```python
    async def write(self, uri: str, content: str) -> None:
        """原子写：同目录 tmp + os.replace；自动 mkdir -p 父目录。"""
        path = self.resolve(uri)
        parent = os.path.dirname(path)
        async with self._lock_for(path):
            os.makedirs(parent, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                os.replace(tmp, path)                  # POSIX 原子 rename
            except BaseException:
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
```

- [ ] **Step 4：跑，确认通过** —— Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_vfs.py -q`
  Expected: 全 passed。

- [ ] **Step 5：Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/memory/vfs.py tests/test_auth/test_memory_vfs.py
git commit -m "$(cat <<'EOF'
feat(memory): S1 MemoryFS write(原子)/read/exists（TDD）

write: 同目录 .tmp + os.replace 原子落盘 + 自动建父目录 + 失败清 tmp；
read 不存在抛 MemoryNotFound、目录抛 MemoryPathError；exists。设计 §1.4。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4：`ls`（三态）+ `rm`（recursive）+ `mv`（同 user，跨 user 拒）

**Files:** Modify `src/service/memory/vfs.py`; Test `tests/test_auth/test_memory_vfs.py`

- [ ] **Step 1：写失败测试** —— 追加到 `tests/test_auth/test_memory_vfs.py` 末尾：

```python
@pytest.mark.asyncio
async def test_ls_three_states(tmp_path):
    fs = _fs(tmp_path)
    with pytest.raises(MemoryNotFound):
        await fs.ls("ke://u/7/global")                 # 不存在
    await fs.write("ke://u/7/global/a.md", "c")
    assert await fs.ls("ke://u/7/global") == ["a.md"]  # 有内容
    await fs.write("ke://u/7/global/b.md", "c")
    assert await fs.ls("ke://u/7/global") == ["a.md", "b.md"]  # 排序
    with pytest.raises(MemoryPathError):
        await fs.ls("ke://u/7/global/a.md")            # 非目录


@pytest.mark.asyncio
async def test_rm_file_and_missing(tmp_path):
    fs = _fs(tmp_path)
    await fs.write("ke://u/7/global/a.md", "c")
    await fs.rm("ke://u/7/global/a.md")
    assert await fs.exists("ke://u/7/global/a.md") is False
    with pytest.raises(MemoryNotFound):
        await fs.rm("ke://u/7/global/a.md")


@pytest.mark.asyncio
async def test_rm_dir_needs_recursive(tmp_path):
    fs = _fs(tmp_path)
    await fs.write("ke://u/7/project/p1/x.md", "c")
    with pytest.raises(MemoryPathError):
        await fs.rm("ke://u/7/project/p1")             # 目录但未 recursive
    await fs.rm("ke://u/7/project/p1", recursive=True)
    assert await fs.exists("ke://u/7/project/p1") is False


@pytest.mark.asyncio
async def test_mv_within_user(tmp_path):
    fs = _fs(tmp_path)
    await fs.write("ke://u/7/global/a.md", "hello")
    await fs.mv("ke://u/7/global/a.md", "ke://u/7/project/p1/a.md")
    assert await fs.exists("ke://u/7/global/a.md") is False
    assert await fs.read("ke://u/7/project/p1/a.md") == "hello"


@pytest.mark.asyncio
async def test_mv_cross_user_rejected(tmp_path):
    fs = _fs(tmp_path)
    await fs.write("ke://u/7/global/a.md", "hello")
    with pytest.raises(MemoryPathError):
        await fs.mv("ke://u/7/global/a.md", "ke://u/8/global/a.md")
    assert await fs.exists("ke://u/7/global/a.md") is True  # 源未动


@pytest.mark.asyncio
async def test_mv_missing_src_not_found(tmp_path):
    fs = _fs(tmp_path)
    with pytest.raises(MemoryNotFound):
        await fs.mv("ke://u/7/global/nope.md", "ke://u/7/global/b.md")
```

- [ ] **Step 2：跑，确认失败** —— Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_vfs.py -q -k "ls or rm or mv"`
  Expected: FAIL —— `AttributeError: ... has no attribute 'ls'`。

- [ ] **Step 3：实现** —— 在 `MemoryFS` 类内（`exists` 之后）追加方法：

```python
    async def ls(self, uri: str) -> list[str]:
        path = self.resolve(uri)
        if not os.path.exists(path):
            raise MemoryNotFound(uri)
        if not os.path.isdir(path):
            raise MemoryPathError(f"not a directory: {uri!r}")
        return sorted(os.listdir(path))

    async def rm(self, uri: str, *, recursive: bool = False) -> None:
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
        if self._uid_of(src_uri) != self._uid_of(dst_uri):
            raise MemoryPathError(
                f"cross-user mv forbidden: {src_uri!r} -> {dst_uri!r}")
        src = self.resolve(src_uri)
        dst = self.resolve(dst_uri)
        async with self._lock_for(src):
            if not os.path.exists(src):
                raise MemoryNotFound(src_uri)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
```

- [ ] **Step 4：跑，确认通过** —— Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_vfs.py -q`
  Expected: 全 passed。

- [ ] **Step 5：Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/memory/vfs.py tests/test_auth/test_memory_vfs.py
git commit -m "$(cat <<'EOF'
feat(memory): S1 MemoryFS ls(三态)/rm(recursive)/mv(同 user，跨 user 拒)（TDD）

ls 不存在→MemoryNotFound、非目录→MemoryPathError、空→[]（sorted）；
rm 目录需 recursive；mv 先比 uid 段跨 user 即拒、源不存在→MemoryNotFound。
设计 §1.4。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5：单实例并发——同路径写经锁串行最终一致

**Files:** Test `tests/test_auth/test_memory_vfs.py`（仅加测试，验证 Task3 已实现的 per-path 锁；如测试暴露缺陷再修 `vfs.py`）

- [ ] **Step 1：写失败/验证测试** —— 追加到 `tests/test_auth/test_memory_vfs.py` 末尾：

```python
@pytest.mark.asyncio
async def test_concurrent_same_path_writes_serialized_consistent(tmp_path):
    fs = _fs(tmp_path)
    uri = "ke://u/7/global/c.md"

    async def w(v: str) -> None:
        await fs.write(uri, v)

    await asyncio.gather(*[w(f"val-{i}") for i in range(20)])
    # 经 per-path asyncio.Lock 串行：最终内容必是某一次完整写入，非交错半文件
    final = await fs.read(uri)
    assert final in {f"val-{i}" for i in range(20)}
    d = os.path.join(str(tmp_path), "u", "7", "global")
    assert os.listdir(d) == ["c.md"]                   # 无 .tmp 残留


@pytest.mark.asyncio
async def test_concurrent_distinct_paths_all_written(tmp_path):
    fs = _fs(tmp_path)

    async def w(i: int) -> None:
        await fs.write(f"ke://u/7/global/f{i}.md", str(i))

    await asyncio.gather(*[w(i) for i in range(15)])
    for i in range(15):
        assert await fs.read(f"ke://u/7/global/f{i}.md") == str(i)
```

- [ ] **Step 2：跑，确认通过** —— Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_vfs.py -q -k concurrent`
  Expected: PASS（Task3 的 per-path `asyncio.Lock` + 原子 replace 已保证）。若 FAIL：说明锁/原子写有缺陷——按 systematic-debugging 定位修 `vfs.py`（不可改测试意图），修后全绿。

- [ ] **Step 3：Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add tests/test_auth/test_memory_vfs.py
git commit -m "$(cat <<'EOF'
test(memory): S1 单实例并发——同路径写经锁串行最终一致 + 异路径并行（TDD）

锁定 per-path asyncio.Lock + 原子 replace 的并发不变量。设计 §1.4。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6：回归 + import 自检（controller，无新文件/commit）

- [ ] **Step 1：S1 全套** —— Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_vfs.py -q`
  Expected: 全 passed（Task1-5 累计 ~30 用例）。

- [ ] **Step 2：import 自检** —— Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -c "from src.service.memory.vfs import MemoryFS, MemoryPathError, MemoryNotFound, mem_root; print('OK', mem_root().endswith('.ke-memory'))"`
  Expected: `OK True`（未设 KE_MEM_ROOT 时）。

- [ ] **Step 3：既有记忆/QA 链路不回归** —— Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/ -q -k "memory or qa or prompt or chitchat or sse" -p no:warnings`
  Expected: 0 fail（S1 纯新增 `vfs.py` + 新测试文件，未改任何既有模块，零回归）。

> 端到端不在 S1 范围（S1 是地基，无 router/SSE 接入；接入在 S3/S4/S6）。

---

## Self-Review（实施者过一遍）

- [ ] spec §1 逐条：§1.1 URI（ke://u/{uid}/global|project/{pid}|session/{sid}）→ resolve 测试覆盖 ✓；§1.2 段白名单+`.`/`..`精确拒+前导点允许+realpath 前缀断言+符号链接逃逸 → Task2 ✓；§1.3 KE_MEM_ROOT+缺省 .ke-memory → Task1 ✓；§1.4 API（resolve/read/write 原子/exists/ls 三态/rm recursive/mv 同 user）→ Task2-4 ✓ + per-path 锁 Task3 实现/Task5 验证 ✓；§1.5 清晰抛错不静默 → MemoryPathError/MemoryNotFound 贯穿 ✓；§1.6 不做 S2-S6 → 计划无 L0/L1/L2/召回/抽取/迁移 ✓
- [ ] 占位扫描：每 code step 完整可粘贴、命令带 Expected、无 TBD/“类似 Task” ✓
- [ ] 类型一致：`MemoryFS(root=...)`、`resolve/read/write/exists/ls/rm(recursive=)/mv`、`mem_root()`、`MemoryPathError`/`MemoryNotFound`、`_uid_of`/`_lock_for`/`_user_base`/`_SEG_RE`/`_UID_RE`/`_URI_PREFIX` 全计划同名一致；测试 helper `_fs(tmp_path)` 一致 ✓
- [ ] YAGNI：仅 S1 地基；无跨实例锁、无索引联动、无 anyio/aiofiles 新依赖（async 方法 + 同步 IO）✓

## Phase Definition of Done

- [ ] S1 全套 ~30 用例全绿（resolve 穿越/隔离/符号链接、原子写、ls 三态、rm、mv 跨 user 拒、并发串行）
- [ ] import OK；既有 memory/qa 链路 0 回归
- [ ] 5 feat/test commit 干净（骨架 / resolve / write-read-exists / ls-rm-mv / 并发）
- [ ] 仅新增 `src/service/memory/vfs.py` + `tests/test_auth/test_memory_vfs.py`，未改既有文件
