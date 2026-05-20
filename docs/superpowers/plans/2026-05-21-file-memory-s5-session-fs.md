# 文件式记忆重构 S5 — 会话级文件化 + 读侧 composer 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `qa_session_memory.working_summary` 从 DB tier 迁到 `ke://u/{uid}/session/{sid}/summary.md` 文件存储；同时补齐 §4.3 长期 orphan 的读侧 composer，落实「trim 后旧轮历史由 working_summary 顶替」的设计原意。

**Architecture:** 新增 `src/service/memory/session.py` 含 `SessionCompactor` 类（同 S4 `MemoryExtractor` 架构）+ `read_session_summary` free function（同 S3 `recall_memory_block` wrapper 模式）。写侧 post-turn 闭包内 `SessionCompactor(llm).compact(fs, db, ...)` 替换 `maybe_compact_session`；读侧 `qa_router` 在 `recall_memory_block` 之后再调一次 `read_session_summary` 把 session summary 拼到 `memory_block` 头部。`QASessionMemory` ORM 类 + `maybe_compact_session` 函数删除；SQL 表 stranded 留 S6 「DB 下线」一起 Alembic migration。

**Tech Stack:** Python 3.12 / pytest / pytest-asyncio / SQLAlchemy ORM (read-only on QAMessage) / S1 `MemoryFS` (vfs.py) / S2 frontmatter helpers (memgen.py) / S4 `_now_iso_z` (extract.py reuse) / 既有 `_SESSION_COMPACT_SYSTEM` prompt 不动。

**Spec source:** Obsidian `/Users/java/obsidian/01 Engineering/knowledge-engineering/文件式记忆重构-设计.md` §6（§6.0–§6.9）。

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `src/service/memory/session.py` | **Create** | `SessionCompactor` 类 + `read_session_summary` free function + `_summary_uri` helper（`_now_iso_z` 从 S4 extract.py import 复用） |
| `tests/test_auth/test_memory_session.py` | **Create** | 13 测试场景（首压 / 递归累积 / floor / delta / force / LLM 返空 / frontmatter 自愈 / focus 持久化 / 跨租户隔离 / 失败隔离 / read 不存在 / read YAML 损坏 / 端到端） |
| `src/service/memory/service.py` | Modify | 删 `maybe_compact_session` 函数（line 100–180）+ 改 import line 14 拆 `QASessionMemory` 移除 |
| `src/service/db_models_homepage.py` | Modify | 删 `QASessionMemory` 类（line 360–388） |
| `src/service/qa_router.py` | Modify | 改 import line 43–46 移除 `maybe_compact_session`；line 283 后加读侧 5b/5c 注入段；line 596–602 写侧调用替换为 SessionCompactor.compact |
| `src/service/qa_engine/synthesizer.py` | Modify | 注释清理 line 84/111「memory_block 的 working_summary 顶替」改事实陈述 |
| `src/service/memory/context_budget.py` | Modify | 注释清理 line 4 同上 |
| `tests/test_auth/test_memory_service.py` | Modify | 删 11 `maybe_compact_session` 测试 + 改 import line 9-12 + 清理 `_FakeMemDB` 中 QASessionMemory 基础设施 |
| `tests/test_auth/test_models_memory.py` | Modify | 删 4 `QASessionMemory` 测试（line 43–69）+ 改 import line 7 |
| `tests/test_auth/test_qa_router.py` | Modify | mock 点 line 419–422 改为 patch `src.service.memory.session.SessionCompactor` |

---

## Task 1: session.py 骨架 + helpers + read_session_summary

**Files:**
- Create: `src/service/memory/session.py`
- Create: `tests/test_auth/test_memory_session.py`

**Why this task:** 建立 S5 模块骨架，定义 `_summary_uri` URI 派生器、`SessionCompactor` 类 shell（仅 `__init__` + `compact` 方法签名 stub）、`read_session_summary` 完整实现。`compact` 的算法体留到 T2。先建 read 路径完成端：read 不依赖 compact 已实现，可单独测试 + 服务于读侧 composer，T2 写完后读写形成完整链路。

### Step 1: 写 `_summary_uri` 单元测试

- [ ] **Step 1: Write the failing test**

```python
# tests/test_auth/test_memory_session.py（新建）
"""文件式记忆 S5：SessionCompactor + read_session_summary 单测。
设计：[[文件式记忆重构-设计]] §6。
沿用 tests/test_auth 既有 fake + tmp_path + @pytest.mark.asyncio 风格；
跨 S2/S4 fake stack 复用（_split_frontmatter/_render_frontmatter from memgen，
fake LLM 模式同 test_memory_extract.py）。
"""
from __future__ import annotations

import pytest

from src.service.memory.session import _summary_uri


def test_summary_uri_basic():
    """_summary_uri 拼正确的 ke:// URI（§6.2）。"""
    assert _summary_uri(7, "sess_abc") == "ke://u/7/session/sess_abc/summary.md"


def test_summary_uri_different_users_isolated():
    """不同 user_id 派生不同路径（S1 路径前缀隔离的基础）。"""
    u1 = _summary_uri(1, "s")
    u2 = _summary_uri(2, "s")
    assert u1 != u2
    assert "/u/1/" in u1 and "/u/2/" in u2


def test_summary_uri_different_sessions_isolated():
    """同 user 不同 session 派生不同路径。"""
    s1 = _summary_uri(7, "sess_a")
    s2 = _summary_uri(7, "sess_b")
    assert s1 != s2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m pytest tests/test_auth/test_memory_session.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.service.memory.session'`

- [ ] **Step 3: 创建 session.py 模块 + `_summary_uri`**

```python
# src/service/memory/session.py（新建）
"""文件式记忆 S5：会话级 working_summary 文件化压缩器 + 读侧 composer。

设计：[[文件式记忆重构-设计]] §6（§6.0–§6.9）。纯逻辑，不依赖 FastAPI；
DB 用 duck-typed AsyncSession（真跑用 SQLAlchemy，单测用 Fake），
LLM 用 duck-typed provider（鸭子 async complete(system,user,**kw)->str）。

S5 公开 API（§6.2）：
- ``SessionCompactor(llm).compact(fs, db, *, user_id, session_id, ...)`` — 写侧
- ``read_session_summary(fs, *, user_id, session_id) -> str`` — 读侧
- ``_summary_uri(user_id, session_id) -> str`` — URI 派生 helper

`maybe_compact_session` (service.py) 的 1:1 语义替换：算法与守卫 verbatim 保留
（§6.4 数据流），仅把 DB↔fs 交换。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from src.service.db_models_homepage import QAMessage
from src.service.memory.vfs import MemoryFS, MemoryNotFound
from src.service.memory.memgen import _render_frontmatter, _split_frontmatter
from src.service.memory.service import _extract_focus_entity_ids
from src.service.memory.extract import _now_iso_z          # S4 既有 helper，原样复用
from src.service.qa_engine.prompts import _SESSION_COMPACT_SYSTEM

# 模块级 logger（与 vfs.py / memgen.py / recall.py / extract.py 同模式）
_log = logging.getLogger(__name__)


def _summary_uri(user_id: int, session_id: str) -> str:
    """URI 派生 helper：ke://u/{uid}/session/{sid}/summary.md（§6.2 唯一路径形态）。

    本函数纯字符串拼装，不调 fs.resolve（safe 校验留给 fs.read/fs.write）；
    user_id 是 KE Integer（≥1，DB 自增），session_id 是 KE String(64)（业务串）。
    """
    # f-string 拼 ke:// 前缀 + /u/{uid}/session/{sid}/summary.md
    return f"ke://u/{user_id}/session/{session_id}/summary.md"
```

- [ ] **Step 4: Run tests to verify pass**

Run: `./venv/bin/python -m pytest tests/test_auth/test_memory_session.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/service/memory/session.py tests/test_auth/test_memory_session.py
git commit -m "$(cat <<'EOF'
feat(memory-s5): session.py 骨架 + _summary_uri helper

S5 T1 step 1: 模块 docstring + 顶部 imports + _summary_uri URI 派生。
设计 §6.2 唯一路径形态：ke://u/{uid}/session/{sid}/summary.md。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 2: 写 `read_session_summary` 单元测试（不存在路径）

- [ ] **Step 1: Write the failing test**

追加到 `tests/test_auth/test_memory_session.py`：

```python
from src.service.memory.session import read_session_summary
from src.service.memory.vfs import MemoryFS


@pytest.mark.asyncio
async def test_read_session_summary_not_exists_returns_empty(tmp_path):
    """summary.md 不存在 → 返 ""（与 recall_memory_block 同自包失败语义，§6.5）。"""
    # tmp_path 是 pytest 内置的临时目录 fixture；MemoryFS(root=...) 接收物理根
    fs = MemoryFS(root=str(tmp_path))
    # user 7 + sess_x 路径下没写过任何文件
    result = await read_session_summary(fs, user_id=7, session_id="sess_x")
    # 不存在 → 返 "" 让 composer 走零开销不注入路径
    assert result == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m pytest tests/test_auth/test_memory_session.py::test_read_session_summary_not_exists_returns_empty -v`
Expected: FAIL with `ImportError: cannot import name 'read_session_summary'`

- [ ] **Step 3: 实现 `read_session_summary`**

追加到 `src/service/memory/session.py`（在 `_summary_uri` 之后）：

```python
async def read_session_summary(
    fs: MemoryFS, *, user_id: int, session_id: str,
) -> str:
    """读 session summary.md 的 body 段（去 frontmatter）。

    设计：[[文件式记忆重构-设计]] §6.4 读侧算法。
    不存在 / 失败 → 返 ""（与 recall_memory_block 同自包失败语义，§6.5）。
    composer 直接拼到 memory_block 头部（qa_router 5b/5c 段，T4 接入）。

    frontmatter 损坏时 `_split_frontmatter` 降级返 ({}, 全文)，
    本函数取 body — body 是 frontmatter 闭合后的部分；闭合都没探到时
    `_split_frontmatter` 返 ({}, 原文)，本函数仍返裸文本作 summary（自愈优先，§6.5）。
    """
    try:
        # 拼路径（§6.2 唯一形态）
        uri = _summary_uri(user_id, session_id)
        # fs.read 不存在抛 MemoryNotFound（vfs.py read 契约）
        raw = await fs.read(uri)
        # S2 helper 拆 frontmatter；非法 YAML 已被 S2 内部容错为空 dict
        # body 是闭合后的部分（或无 frontmatter 时即全文）
        _meta, body = _split_frontmatter(raw)
        # strip 去尾换行（_render_frontmatter 写时带 "\n"，读时去尾保持纯文本）
        return (body or "").strip()
    except MemoryNotFound:
        # 首次会话尚无 summary.md 是正常路径，非异常；返 "" 让 composer 跳注入
        return ""
    except Exception as exc:                              # 任何其他异常深度兜底
        # 中层失败语义（§6.5）：debug 留痕 + 返 ""，不抛
        _log.debug("read_session_summary failed: %r", exc)
        return ""
```

- [ ] **Step 4: Run test to verify pass**

Run: `./venv/bin/python -m pytest tests/test_auth/test_memory_session.py::test_read_session_summary_not_exists_returns_empty -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/service/memory/session.py tests/test_auth/test_memory_session.py
git commit -m "$(cat <<'EOF'
feat(memory-s5): read_session_summary 自包失败语义 + 不存在返 ""

S5 T1 step 2：composer 读侧 free function（同 S3 recall_memory_block 模式）。
设计 §6.4 读侧算法 + §6.5 自包失败语义（不存在/损坏/异常 → 返 ""）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 3: 写 `read_session_summary` YAML 损坏自愈测试

- [ ] **Step 1: Write the failing test**

追加到 `tests/test_auth/test_memory_session.py`：

```python
@pytest.mark.asyncio
async def test_read_session_summary_corrupt_frontmatter_returns_body(tmp_path):
    """frontmatter YAML 损坏但 body 可读 → 返裸 body（自愈优先，§6.5）。

    场景：S5 部署初期 / 手工编辑误 / partial write 崩溃产出半损坏文件；
    composer 不能因为 frontmatter 坏就丢 summary 内容（用户体验降级）。
    """
    fs = MemoryFS(root=str(tmp_path))
    # 手写一份「frontmatter YAML 损坏 + body 完整」的文件直接落盘
    # _split_frontmatter 见非法 YAML 容错为空 dict，body 仍正确返
    bad_yaml = "---\n: : : invalid yaml ::: \n---\n用户讨论 PaymentGateway。\n"
    # 直接写文件（绕开 _render_frontmatter，模拟外部损坏）
    await fs.write("ke://u/7/session/sess_x/summary.md", bad_yaml)

    result = await read_session_summary(fs, user_id=7, session_id="sess_x")
    # body 仍可读 → 返裸文本（去尾换行）
    assert result == "用户讨论 PaymentGateway。"
```

- [ ] **Step 2: Run test to verify**

Run: `./venv/bin/python -m pytest tests/test_auth/test_memory_session.py::test_read_session_summary_corrupt_frontmatter_returns_body -v`
Expected: PASS（既有 read_session_summary 已实现，S2 `_split_frontmatter` 容错已做）

- [ ] **Step 3: Commit**

```bash
git add tests/test_auth/test_memory_session.py
git commit -m "$(cat <<'EOF'
test(memory-s5): read_session_summary frontmatter 损坏自愈回归

设计 §6.5 自愈优先：YAML 损坏但 body 可读 → 返裸 body，
让 composer 用户体验不因元数据损坏而丢 summary 内容。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 4: 写 `SessionCompactor.__init__` 单元测试

- [ ] **Step 1: Write the failing test**

追加到 `tests/test_auth/test_memory_session.py`：

```python
from src.service.memory.session import SessionCompactor


class _FakeLLM:
    """记录 complete() 调用入参；返回固定的 fake summary 文本。

    与 test_memory_extract.py 既有 fake LLM 同形态（鸭子 async complete）。
    """
    def __init__(self, *, response: str = "fake summary 文本"):
        self.calls: list[dict] = []
        self.response = response

    async def complete(self, *, system: str, user: str, **kw) -> str:
        # 记录每次调用的 system / user 参数（断言用）
        self.calls.append({"system": system, "user": user})
        return self.response


def test_session_compactor_init_holds_llm():
    """SessionCompactor 仅持 llm（同 S4 MemoryExtractor，fs/db 走方法形参）。"""
    llm = _FakeLLM()
    compactor = SessionCompactor(llm)
    # 不直接访问私有 _llm 字段（视为实现细节）；通过断言无异常构造来约束公开契约
    assert compactor is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m pytest tests/test_auth/test_memory_session.py::test_session_compactor_init_holds_llm -v`
Expected: FAIL with `ImportError: cannot import name 'SessionCompactor'`

- [ ] **Step 3: 实现 `SessionCompactor` 类 shell（init 完整，compact stub）**

追加到 `src/service/memory/session.py`（在 `read_session_summary` 之后）：

```python
class SessionCompactor:
    """会话级摘要文件化压缩器（替代 maybe_compact_session）。

    设计：[[文件式记忆重构-设计]] §6（§6.2/§6.4）。同 S4 MemoryExtractor 架构：
    ``__init__`` 仅绑 llm，fs/db 走方法形参（便于测试 fake，便于 S7 singletonize）。

    单例策略：S7 把 SessionCompactor + MemoryExtractor 一同提到 module-level
    singleton；S5 仍在闭包内构造一次（成本可忽略；与 S4 同模式）。
    """

    def __init__(self, llm) -> None:
        """绑 LLM provider（鸭子 ``async complete(system,user,**kw)->str``）。"""
        # 仅持 llm；fs/db 走 compact() 形参（§6.2 接口契约）
        self._llm = llm

    async def compact(
        self,
        fs: MemoryFS,
        db: Any,
        *,
        user_id: int,
        session_id: str,
        every_n_messages: int = 6,
        force: bool = False,
    ) -> None:
        """post-turn 触发的会话压缩。算法 §6.4（T2 实现完整体）。"""
        # T2 step 1: 完整实现见后续提交；此处保留 stub 让 T1 测试可链接
        raise NotImplementedError("compact() implementation lands in S5 T2")
```

- [ ] **Step 4: Run test to verify pass**

Run: `./venv/bin/python -m pytest tests/test_auth/test_memory_session.py::test_session_compactor_init_holds_llm -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/service/memory/session.py tests/test_auth/test_memory_session.py
git commit -m "$(cat <<'EOF'
feat(memory-s5): SessionCompactor 类 shell（__init__ + compact stub）

S5 T1 step 4：类接口锁定签名，compact 实现留 T2。
设计 §6.2 接口契约（同 S4 MemoryExtractor 架构）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 5: Task 1 完整回归

- [ ] **Step 1: 运行 T1 全部测试**

Run: `./venv/bin/python -m pytest tests/test_auth/test_memory_session.py -v`
Expected: 5 passed（3 _summary_uri + 1 read 不存在 + 1 read YAML 损坏 + 1 SessionCompactor init = wait 是 6 个，但 init 后到 T2 之间还会再加。重数下：test_summary_uri_basic / test_summary_uri_different_users_isolated / test_summary_uri_different_sessions_isolated / test_read_session_summary_not_exists_returns_empty / test_read_session_summary_corrupt_frontmatter_returns_body / test_session_compactor_init_holds_llm = 6 tests）

- [ ] **Step 2: 与 S4 既有测试同跑确认 module import 无副作用**

Run: `./venv/bin/python -m pytest tests/test_auth/test_memory_extract.py tests/test_auth/test_memory_session.py -v`
Expected: 18 (extract) + 6 (session) = 24 passed

- [ ] **Step 3: 不另增 commit（T1 闭合 review 时统一）**

T1 出口标准：T1 共 4 个 commit（_summary_uri / read 不存在 / read YAML 损坏 / SessionCompactor init），共 6 passed test。

---

## Task 2: SessionCompactor.compact 核心算法

**Files:**
- Modify: `src/service/memory/session.py:64–80`（`compact` 方法体替换 stub）
- Modify: `tests/test_auth/test_memory_session.py`（追加 7 测试场景）

**Why this task:** T2 落 §6.4 完整算法 step 1–8（拉 messages → floor → 读旧 → delta → 拼 convo → LLM → focus → 写文件 + 整体 try/except）。验证 7 个非 focus / 非端到端 / 非跨租户 / 非失败隔离场景（floor/delta/force/LLM 返空/首压/递归/frontmatter 自愈）。focus + 端到端 + 跨租户 + 失败隔离留 T3。

### Step 1: 写首压路径测试

- [ ] **Step 1: 在 test_memory_session.py 顶部加 fake fixtures（fake QAMessage + fake DB session）**

追加到 `tests/test_auth/test_memory_session.py`（在已有 imports 后）：

```python
from dataclasses import dataclass, field
from typing import Any as _Any


@dataclass
class _FakeMessage:
    """fake QAMessage row（duck-type：role / content / msg_metadata / created_at）。

    设计：仅满足 SessionCompactor.compact step 1 select 之后的 ORM-like
    属性访问 + step 7 _extract_focus_entity_ids 需要的 msg_metadata 字段。
    """
    role: str
    content: str
    msg_metadata: dict | None = None
    created_at: _Any = None


class _FakeMsgScalars:
    """fake .scalars() 返回值：仅暴露 .all() → list[_FakeMessage]。"""
    def __init__(self, rows: list[_FakeMessage]):
        self._rows = rows

    def all(self) -> list[_FakeMessage]:
        return self._rows


class _FakeMsgResult:
    """fake db.execute() 返回值：仅暴露 .scalars() → _FakeMsgScalars。"""
    def __init__(self, rows: list[_FakeMessage]):
        self._rows = rows

    def scalars(self) -> _FakeMsgScalars:
        return _FakeMsgScalars(self._rows)


class _FakeDB:
    """fake AsyncSession：仅响应 execute(select(QAMessage).where(...).order_by(...))。

    不实现 commit/add/flush；S5 fs 落盘不走 DB 写路径，DB 仅读 QAMessage（S6 才迁）。
    """
    def __init__(self, messages: list[_FakeMessage]):
        self._messages = messages

    async def execute(self, stmt) -> _FakeMsgResult:
        # stmt 是 SQLAlchemy select 表达式，我们不解析（fake 只服务 compact 的单条 select）
        # 返回固定 messages（每次测试单独构造 _FakeDB(messages=...)）
        return _FakeMsgResult(self._messages)


def _msgs(*roles_contents: tuple[str, str]) -> list[_FakeMessage]:
    """便捷构造一批 fake message。

    用法：_msgs(("user", "你好"), ("assistant", "你好！"), ...)
    """
    return [_FakeMessage(role=r, content=c) for r, c in roles_contents]
```

- [ ] **Step 2: Write the failing test**

追加到 `tests/test_auth/test_memory_session.py`：

```python
@pytest.mark.asyncio
async def test_compact_first_time_writes_new_summary(tmp_path):
    """首压路径：summary.md 不存在 + msg_count=6 + every_n=6（§6.7 场景 1）。

    fs.read 抛 MemoryNotFound → prev_turn_count=0 / prev_summary=""
    → 拼 convo 仅含【新增对话】段 → 调 LLM → 写新 summary.md。
    断言：frontmatter.turn_count == 6，body 含 LLM 输出。
    """
    fs = MemoryFS(root=str(tmp_path))
    db = _FakeDB(_msgs(*[("user", f"q{i}") if i % 2 == 0 else ("assistant", f"a{i}") for i in range(6)]))
    llm = _FakeLLM(response="用户讨论了 6 条消息，主要话题是 q0/q2/q4。")
    compactor = SessionCompactor(llm)

    await compactor.compact(fs, db, user_id=7, session_id="sess_first", every_n_messages=6)

    # 断言文件已写
    uri = "ke://u/7/session/sess_first/summary.md"
    assert await fs.exists(uri)
    raw = await fs.read(uri)
    # frontmatter 解析回来验证 turn_count
    from src.service.memory.memgen import _split_frontmatter
    meta, body = _split_frontmatter(raw)
    assert meta["turn_count"] == 6
    # body 含 LLM 输出（去尾换行后）
    assert "用户讨论了 6 条消息" in body
    # LLM 被调用 1 次
    assert len(llm.calls) == 1
    # convo 不含【已有会话摘要】（首压）
    assert "【已有会话摘要】" not in llm.calls[0]["user"]
    # convo 含【新增对话】
    assert "【新增对话】" in llm.calls[0]["user"]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `./venv/bin/python -m pytest tests/test_auth/test_memory_session.py::test_compact_first_time_writes_new_summary -v`
Expected: FAIL with `NotImplementedError: compact() implementation lands in S5 T2`

- [ ] **Step 4: 实现 `SessionCompactor.compact` 完整算法**

替换 `src/service/memory/session.py` 的 `compact` 方法体（删 `raise NotImplementedError(...)` 那一行，换为完整实现）：

```python
    async def compact(
        self,
        fs: MemoryFS,
        db: Any,
        *,
        user_id: int,
        session_id: str,
        every_n_messages: int = 6,
        force: bool = False,
    ) -> None:
        """post-turn 触发的会话压缩。

        设计 §6.4 verbatim 8 步算法：拉 messages → floor → 读旧 summary →
        delta → 拼 convo → LLM → focus → 写新 summary.md。

        保留旧 ``maybe_compact_session`` (service.py) 全部守卫语义：
        - floor 判定（force=True 时降低门槛到 2，否则 every_n_messages）
        - delta 判定（自上次压缩起新增 ≥ N；force=True 时 ≥ 1）
        - LLM 返空早退（§6.5）
        - 整体 try/except → _log.debug 静默（记忆是辅助，§4.3）
        """
        try:
            # ─── step 1: 拉本 session 所有 messages（仍 DB；QAMessage 文件化属 S6） ───
            # select(QAMessage).where(...).order_by(...) 沿用 service.py 既有写法
            msg_res = await db.execute(
                select(QAMessage)
                .where(QAMessage.session_id == session_id)
                .order_by(QAMessage.created_at)
            )
            # .scalars().all() 把 Row 对象拆为列扁平 list（ORM 单实体 select 标准用法）
            messages = msg_res.scalars().all()
            msg_count = len(messages)

            # ─── step 2: floor 判定（与旧版 maybe_compact_session 同：force 降门槛到 2） ───
            # force=True（上下文压力 spec §18）：越过固定 N floor，但仍要求 msg_count ≥ 2
            floor = 2 if force else every_n_messages
            if msg_count < floor:
                # 消息数不足 floor → 不调 LLM、不读 fs、不写 fs（成本守卫）
                return

            # ─── step 3: 读旧 summary.md（不存在 → 首压，prev_turn_count=0, prev_summary="") ───
            uri = _summary_uri(user_id, session_id)
            prev_turn_count = 0
            prev_summary = ""
            try:
                # fs.read 不存在抛 MemoryNotFound（vfs.py read 契约）
                raw = await fs.read(uri)
                # S2 helper 拆 frontmatter；非法 YAML 已被 S2 内部容错为空 dict {}
                fm, body = _split_frontmatter(raw)
                # body 是 frontmatter 闭合后的部分（或无 frontmatter 时即全文）
                prev_summary = (body or "").strip()
                # fm 由 _split_frontmatter 保证是 dict（空 YAML / 损坏 → {}），
                # 故只需检查 turn_count 字段类型 + ≥0 取值（防手工误改产出 -5 等）
                tc = fm.get("turn_count")
                if isinstance(tc, int) and tc >= 0:
                    prev_turn_count = tc
            except MemoryNotFound:
                # 首压路径：summary.md 还不存在 → prev_turn_count 维持 0
                pass

            # ─── step 4: delta 判定（自上次压缩起新增 ≥ N；force 降到 ≥ 1） ───
            # 与旧版同：避免「过阈后每轮压缩 = 成本 bug」（消息每轮 +2，过 6 后会每轮跑 LLM）
            min_delta = 1 if force else every_n_messages
            if msg_count - prev_turn_count < min_delta:
                return

            # ─── step 5: 拼 convo：§21 递归累积（旧 summary + 仅自水位线起的新增消息） ───
            # messages[prev_turn_count:] 取自上次压缩水位线起的新增 messages
            # 旧 summary 已被 LLM 浓缩 → 不再二次浓缩老消息，token 输入恒有界
            new_msgs = messages[prev_turn_count:]
            parts: list[str] = []
            if prev_summary:
                parts.append("【已有会话摘要】\n" + prev_summary)
            # 守卫已保证 msg_count - prev_turn_count >= min_delta >= 1 → new_msgs 必非空；
            # 仍以 if 守一层，与 prev_summary 段对称且对未来阈值改动稳健
            if new_msgs:
                parts.append(
                    "【新增对话】\n"
                    + "\n".join(
                        # [role] 前缀 + content 截 200 字（与旧版一致）；
                        # 截断防单条消息撑爆 LLM context；m.content 可能 None → 默认 ""
                        f"[{m.role}] {(m.content or '')[:200]}" for m in new_msgs
                    )
                )
            # 两段间用 \n\n 分隔（同旧版 service.py:152）
            convo = "\n\n".join(parts)

            # ─── step 6: LLM 调用 ───
            summary = await self._llm.complete(system=_SESSION_COMPACT_SYSTEM, user=convo)
            # strip 防 LLM 返带前后空白；None 防鸭子 LLM 实现失误返 None
            summary = (summary or "").strip()
            if not summary:
                # LLM 返空 → 不写文件（旧 summary.md 维持，下轮 delta 守卫重试）
                return

            # ─── step 7: focus_entity_ids 聚合（复用 service.py 既有 helper，S4/S5 共用） ───
            # messages[-12:] 取末 12 条（最近 ~6 轮）做 focus 主题判定；
            # _extract_focus_entity_ids 内部已防御 missing/bad metadata，不抛
            focus = _extract_focus_entity_ids(messages[-12:])

            # ─── step 8: 写新 summary.md（frontmatter + body，复用 S2 helper） ───
            fm_new = {
                "turn_count": msg_count,                  # 新水位线
                "focus_entity_ids": focus,                # 截 _FOCUS_MAX=10
                "updated_at": _now_iso_z(),               # ISO 8601 Z（S4 helper 复用）
            }
            # _render_frontmatter(meta, body) 真签名：返 "---\n{YAML}---\n{body}"
            # 调用方约定 body 自带末尾换行（S2 _render_frontmatter docstring 明示）
            content = _render_frontmatter(fm_new, summary + "\n")
            # fs.write 原子写（S1 os.replace POSIX rename）；并发安全
            await fs.write(uri, content)

        except Exception:
            # 中层失败语义（§6.5）：整体 try/except → debug 留痕 + return None，绝不抛
            # 记忆是辅助（§4.3），主答绝不受影响
            _log.debug(
                "SessionCompactor.compact failed for session %s, silently ignored",
                session_id, exc_info=True,
            )
            return
```

- [ ] **Step 5: Run test to verify pass**

Run: `./venv/bin/python -m pytest tests/test_auth/test_memory_session.py::test_compact_first_time_writes_new_summary -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/service/memory/session.py tests/test_auth/test_memory_session.py
git commit -m "$(cat <<'EOF'
feat(memory-s5): SessionCompactor.compact 完整算法 + 首压路径

§6.4 verbatim 8 步：拉 messages → floor → 读旧 → delta → 拼 convo
→ LLM → focus → 写新 summary.md；整体 try/except 中层兜底。
首压路径单元测试：fs.read 抛 MemoryNotFound → prev_turn_count=0
→ 调 LLM → 写新 summary.md（turn_count=6）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 2: 写递归累积路径测试

- [ ] **Step 1: Write the failing test**

追加到 `tests/test_auth/test_memory_session.py`：

```python
@pytest.mark.asyncio
async def test_compact_recursive_folds_prior_summary_and_only_new_msgs(tmp_path):
    """递归累积：summary.md 存在（turn_count=6）+ msg_count=12（§6.7 场景 2）。

    读 prev_summary + 取 messages[6:] 作 new_msgs → 拼 convo（含两段）
    → 调 LLM → 写新（turn_count=12）。
    断言：convo 含【已有会话摘要】+【新增对话】两段；仅 new_msgs 入 convo（旧 6 条不重摘）。
    """
    fs = MemoryFS(root=str(tmp_path))
    uri = "ke://u/7/session/sess_x/summary.md"

    # 预置旧 summary（turn_count=6, body="旧摘要：用户偏好哈密瓜"）
    from src.service.memory.memgen import _render_frontmatter
    prev_content = _render_frontmatter(
        {"turn_count": 6, "focus_entity_ids": [], "updated_at": "2026-05-21T10:00:00Z"},
        "旧摘要：用户偏好哈密瓜\n",
    )
    await fs.write(uri, prev_content)

    # 12 条消息：前 6 已被压缩进 prev_summary；后 6 是新增（带新事实）
    msgs = [_FakeMessage(role="user" if i % 2 == 0 else "assistant",
                          content=f"老消息 {i}" if i < 6 else f"新消息 {i}-讨论西瓜")
            for i in range(12)]
    db = _FakeDB(msgs)
    llm = _FakeLLM(response="更新后摘要：用户偏好哈密瓜+西瓜")
    compactor = SessionCompactor(llm)

    await compactor.compact(fs, db, user_id=7, session_id="sess_x", every_n_messages=6)

    # convo 包含两段
    assert len(llm.calls) == 1
    user_input = llm.calls[0]["user"]
    assert "【已有会话摘要】" in user_input
    assert "旧摘要：用户偏好哈密瓜" in user_input
    assert "【新增对话】" in user_input
    # 旧消息（前 6 条）不入 convo（已被 prev_summary 浓缩）
    assert "老消息 0" not in user_input
    assert "老消息 5" not in user_input
    # 新消息（后 6 条）入 convo
    assert "新消息 6-讨论西瓜" in user_input or "新消息 11-讨论西瓜" in user_input

    # 写后 turn_count=12
    raw = await fs.read(uri)
    from src.service.memory.memgen import _split_frontmatter
    meta, body = _split_frontmatter(raw)
    assert meta["turn_count"] == 12
    assert "更新后摘要" in body
```

- [ ] **Step 2: Run test to verify pass**

Run: `./venv/bin/python -m pytest tests/test_auth/test_memory_session.py::test_compact_recursive_folds_prior_summary_and_only_new_msgs -v`
Expected: PASS（compact 已实现完整，本测试只验证递归路径分支）

- [ ] **Step 3: Commit**

```bash
git add tests/test_auth/test_memory_session.py
git commit -m "$(cat <<'EOF'
test(memory-s5): compact 递归累积 — prev_summary + 仅 new_msgs

§21 递归累积语义 1:1 保留：旧摘要不再重浓缩，token 输入恒有界。
设计 §6.4 step 5 verbatim。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 3: 写守卫测试（floor / delta / force / LLM 返空）

- [ ] **Step 1: Write the 4 failing tests**

追加到 `tests/test_auth/test_memory_session.py`：

```python
@pytest.mark.asyncio
async def test_compact_skips_below_floor(tmp_path):
    """floor 守卫：msg_count=5 + every_n=6 + force=False（§6.7 场景 3）。

    早退（不读 fs，不调 LLM，不写）。
    """
    fs = MemoryFS(root=str(tmp_path))
    msgs = _msgs(*[("user", f"q{i}") for i in range(5)])  # 5 条
    db = _FakeDB(msgs)
    llm = _FakeLLM()
    compactor = SessionCompactor(llm)

    await compactor.compact(fs, db, user_id=7, session_id="sess_x", every_n_messages=6)

    # LLM 未调用 / 文件未写
    assert len(llm.calls) == 0
    assert not await fs.exists("ke://u/7/session/sess_x/summary.md")


@pytest.mark.asyncio
async def test_compact_skips_when_no_new_messages_since_last(tmp_path):
    """delta 守卫：summary.md 存在 turn_count=6 + msg_count=10 + every_n=6（场景 4）。

    10-6=4 < 6 → 早退（避免过阈后每轮压缩 = 成本 bug，对齐旧版守卫）。
    """
    fs = MemoryFS(root=str(tmp_path))
    uri = "ke://u/7/session/sess_x/summary.md"
    from src.service.memory.memgen import _render_frontmatter
    prev = _render_frontmatter(
        {"turn_count": 6, "focus_entity_ids": [], "updated_at": "2026-05-21T10:00:00Z"},
        "旧\n",
    )
    await fs.write(uri, prev)
    msgs = _msgs(*[("user", f"q{i}") for i in range(10)])  # 10 条
    db = _FakeDB(msgs)
    llm = _FakeLLM()
    compactor = SessionCompactor(llm)

    await compactor.compact(fs, db, user_id=7, session_id="sess_x", every_n_messages=6)

    # LLM 未调用
    assert len(llm.calls) == 0
    # 文件未被改写：raw 仍是旧内容
    raw = await fs.read(uri)
    assert "旧" in raw


@pytest.mark.asyncio
async def test_compact_force_bypasses_n_floor(tmp_path):
    """force=True 路径：msg_count=2 + 首压 + force=True（场景 5）。

    floor=2、min_delta=1 → 进 LLM 路径（首压：prev_turn_count=0）。
    """
    fs = MemoryFS(root=str(tmp_path))
    msgs = _msgs(("user", "短"), ("assistant", "回"))
    db = _FakeDB(msgs)
    llm = _FakeLLM(response="强制压缩摘要")
    compactor = SessionCompactor(llm)

    await compactor.compact(fs, db, user_id=7, session_id="sess_x", every_n_messages=6, force=True)

    assert len(llm.calls) == 1
    uri = "ke://u/7/session/sess_x/summary.md"
    raw = await fs.read(uri)
    from src.service.memory.memgen import _split_frontmatter
    meta, body = _split_frontmatter(raw)
    assert meta["turn_count"] == 2
    assert "强制压缩摘要" in body


@pytest.mark.asyncio
async def test_compact_llm_returns_empty_skips_write(tmp_path):
    """LLM 返空：mock LLM 返 "" → 早退 step 6（不写文件）（场景 6）。

    LLM 偶发返空（限流 / token bug）不应破坏既有 summary.md。
    """
    fs = MemoryFS(root=str(tmp_path))
    msgs = _msgs(*[("user", f"q{i}") for i in range(6)])
    db = _FakeDB(msgs)
    llm = _FakeLLM(response="   ")  # 全空白 → strip 后变 "" → 早退
    compactor = SessionCompactor(llm)

    await compactor.compact(fs, db, user_id=7, session_id="sess_x", every_n_messages=6)

    # LLM 被调用 1 次（说明走到 step 6）
    assert len(llm.calls) == 1
    # 文件未写
    assert not await fs.exists("ke://u/7/session/sess_x/summary.md")
```

- [ ] **Step 2: Run tests to verify pass**

Run: `./venv/bin/python -m pytest tests/test_auth/test_memory_session.py -k "skips_below_floor or skips_when_no_new or force_bypasses or llm_returns_empty" -v`
Expected: 4 passed

- [ ] **Step 3: Commit**

```bash
git add tests/test_auth/test_memory_session.py
git commit -m "$(cat <<'EOF'
test(memory-s5): compact 守卫四联（floor / delta / force / LLM 空）

§6.4 守卫语义 1:1 保留：
- floor: msg_count < floor 早退（不读 fs / 不调 LLM / 不写）
- delta: 自水位线起新增 < min_delta 早退（避成本 bug）
- force=True: floor=2 + min_delta=1（context 压力 spec §18）
- LLM 返空: strip 后为 "" → step 6 早退（保旧 summary）

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 4: 写 frontmatter 损坏自愈测试

- [ ] **Step 1: Write the failing test**

追加到 `tests/test_auth/test_memory_session.py`：

```python
@pytest.mark.asyncio
async def test_compact_with_corrupt_frontmatter_self_heals(tmp_path):
    """frontmatter 损坏自愈：手写非法 YAML → prev_turn_count=0 → 重写干净文件（场景 7）。

    场景：S5 部署初期 / 手工编辑误 / partial write 崩溃产出半损坏文件。
    _split_frontmatter 容错为空 dict {} → tc 字段缺失 → prev_turn_count 维持 0
    → delta 守卫按首压路径走（msg_count >= floor + msg_count - 0 >= min_delta）
    → 重新压缩 → 写干净 frontmatter（与 S2 自愈同模式）。
    """
    fs = MemoryFS(root=str(tmp_path))
    uri = "ke://u/7/session/sess_x/summary.md"

    # 手写损坏 YAML 文件（绕开 _render_frontmatter）
    bad = "---\n: : : invalid yaml :::\n---\n旧 body\n"
    await fs.write(uri, bad)

    msgs = _msgs(*[("user", f"q{i}") for i in range(6)])
    db = _FakeDB(msgs)
    llm = _FakeLLM(response="自愈后新摘要")
    compactor = SessionCompactor(llm)

    await compactor.compact(fs, db, user_id=7, session_id="sess_x", every_n_messages=6)

    # LLM 被调用（说明自愈走通）
    assert len(llm.calls) == 1
    # 文件重写为干净 frontmatter
    raw = await fs.read(uri)
    from src.service.memory.memgen import _split_frontmatter
    meta, body = _split_frontmatter(raw)
    assert meta["turn_count"] == 6  # 新水位线
    assert "自愈后新摘要" in body
```

- [ ] **Step 2: Run test to verify pass**

Run: `./venv/bin/python -m pytest tests/test_auth/test_memory_session.py::test_compact_with_corrupt_frontmatter_self_heals -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_auth/test_memory_session.py
git commit -m "$(cat <<'EOF'
test(memory-s5): compact frontmatter 损坏自愈

§6.5 自愈优先：YAML 损坏 → _split_frontmatter 容错为 {} →
prev_turn_count 维持 0 → 重新压缩重写干净 frontmatter
（与 S2 .abstract.md / .overview.md 自愈同模式）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 5: T2 完整回归

- [ ] **Step 1: 运行 T2 全部测试**

Run: `./venv/bin/python -m pytest tests/test_auth/test_memory_session.py -v`
Expected: 6 (T1) + 7 (T2) = 13 passed

- [ ] **Step 2: 与 S4 既有测试同跑确认无相互影响**

Run: `./venv/bin/python -m pytest tests/test_auth/test_memory_extract.py tests/test_auth/test_memory_session.py -v`
Expected: 18 (extract) + 13 (session) = 31 passed

T2 出口：T2 共 4 个 commit（compact 完整算法 + 首压 / 递归 / 守卫四联 / frontmatter 自愈）；13 passed test 全绿。

---

## Task 3: focus_entity_ids 持久化 + 端到端 + 跨租户隔离 + 失败隔离

**Files:**
- Modify: `tests/test_auth/test_memory_session.py`（追加 4 测试场景）

**Why this task:** T3 补 §6.7 剩余 4 个场景（focus 字段写入 / user_id 隔离 / fs.write 失败时 compact 不抛 / write→read 端到端链路）。T3 不改 src/，仅扩测试覆盖；S5 公开 API 已在 T1+T2 落定。

### Step 1: 写 focus_entity_ids 持久化测试

- [ ] **Step 1: Write the failing test**

追加到 `tests/test_auth/test_memory_session.py`：

```python
@pytest.mark.asyncio
async def test_compact_persists_focus_entity_ids(tmp_path):
    """focus_entity_ids 持久化：mock messages 末段含 cited_entities（§6.7 场景 8）。

    _extract_focus_entity_ids(messages[-12:]) 从 msg_metadata.cited_entities
    + entry_points 聚合（service.py 既有，S4/S5 共用）→ 写入 frontmatter。
    """
    fs = MemoryFS(root=str(tmp_path))
    # 末 3 条 assistant 消息带 cited_entities（聚合源）
    msgs = [
        _FakeMessage(role="user", content="q0"),
        _FakeMessage(role="assistant", content="a0"),
        _FakeMessage(role="user", content="q1"),
        _FakeMessage(role="assistant", content="a1",
                      msg_metadata={"cited_entities": ["ent_alpha"]}),
        _FakeMessage(role="user", content="q2"),
        _FakeMessage(role="assistant", content="a2",
                      msg_metadata={"cited_entities": ["ent_beta"], "entry_points": ["ent_gamma"]}),
    ]
    db = _FakeDB(msgs)
    llm = _FakeLLM(response="摘要")
    compactor = SessionCompactor(llm)

    await compactor.compact(fs, db, user_id=7, session_id="sess_x", every_n_messages=6)

    # 文件已写
    uri = "ke://u/7/session/sess_x/summary.md"
    raw = await fs.read(uri)
    from src.service.memory.memgen import _split_frontmatter
    meta, body = _split_frontmatter(raw)
    # focus_entity_ids 按 _extract_focus_entity_ids 的首见顺序去重收集
    focus = meta.get("focus_entity_ids", [])
    assert "ent_alpha" in focus
    assert "ent_beta" in focus
    assert "ent_gamma" in focus
```

- [ ] **Step 2: Run test to verify pass**

Run: `./venv/bin/python -m pytest tests/test_auth/test_memory_session.py::test_compact_persists_focus_entity_ids -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_auth/test_memory_session.py
git commit -m "$(cat <<'EOF'
test(memory-s5): compact focus_entity_ids 持久化

_extract_focus_entity_ids 从 msg_metadata.cited_entities/entry_points
聚合 → frontmatter.focus_entity_ids 字段；S4/S5 共用 helper 不重写。
设计 §6.3 frontmatter schema + §6.4 step 7。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 2: 写跨租户隔离测试

- [ ] **Step 1: Write the failing test**

追加到 `tests/test_auth/test_memory_session.py`：

```python
@pytest.mark.asyncio
async def test_compact_cross_tenant_isolation(tmp_path):
    """跨租户隔离：user_id=1 写 → user_id=2 读不到（§6.7 场景 9）。

    S1 路径前缀隔离自带，本测试是回归保险（防 _summary_uri / fs 误改导致泄漏）。
    """
    fs = MemoryFS(root=str(tmp_path))
    msgs = _msgs(*[("user", f"q{i}") for i in range(6)])
    db = _FakeDB(msgs)
    llm = _FakeLLM(response="user1 的会话摘要")
    compactor = SessionCompactor(llm)

    # user_id=1 写
    await compactor.compact(fs, db, user_id=1, session_id="sess_x", every_n_messages=6)

    # user_id=1 自己读得到
    r1 = await read_session_summary(fs, user_id=1, session_id="sess_x")
    assert "user1 的会话摘要" in r1

    # user_id=2 读同 session_id 拿不到（路径前缀不同）
    r2 = await read_session_summary(fs, user_id=2, session_id="sess_x")
    assert r2 == ""
```

- [ ] **Step 2: Run test to verify pass**

Run: `./venv/bin/python -m pytest tests/test_auth/test_memory_session.py::test_compact_cross_tenant_isolation -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_auth/test_memory_session.py
git commit -m "$(cat <<'EOF'
test(memory-s5): 跨租户隔离回归保险

S1 路径前缀隔离自带（ke://u/{uid}/session/{sid}/）；本测试防
_summary_uri / fs 误改导致泄漏。设计 §6.5 关键不变量第 3 条。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 3: 写 fs.write 失败隔离测试

- [ ] **Step 1: Write the failing test**

追加到 `tests/test_auth/test_memory_session.py`：

```python
@pytest.mark.asyncio
async def test_compact_fs_write_failure_silently_logged(tmp_path, monkeypatch):
    """失败隔离：mock fs.write 抛 → compact 中层 catch → _log.debug → return（不抛）。

    §6.5 关键不变量：summary.md 缺失/损坏永不阻塞 SSE 流。
    """
    fs = MemoryFS(root=str(tmp_path))

    # monkeypatch fs.write 抛任意异常
    async def _explode(*args, **kw):
        raise OSError("simulated disk error")
    monkeypatch.setattr(fs, "write", _explode)

    msgs = _msgs(*[("user", f"q{i}") for i in range(6)])
    db = _FakeDB(msgs)
    llm = _FakeLLM(response="摘要")
    compactor = SessionCompactor(llm)

    # 关键断言：不抛 → compact 中层 catch 兜住
    # （pytest 默认 fail 在未捕获异常 → 不需要 try/except wrap）
    await compactor.compact(fs, db, user_id=7, session_id="sess_x", every_n_messages=6)

    # 文件未写（fs.write 被 mock 抛）
    # 注：此处不能 fs.exists 检查，因为 fs.write mock 后 fs.exists 仍是真的
    # 改为：LLM 仍被调用 1 次（说明 step 1-6 都跑过）
    assert len(llm.calls) == 1
```

- [ ] **Step 2: Run test to verify pass**

Run: `./venv/bin/python -m pytest tests/test_auth/test_memory_session.py::test_compact_fs_write_failure_silently_logged -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_auth/test_memory_session.py
git commit -m "$(cat <<'EOF'
test(memory-s5): compact fs.write 失败中层兜底（不抛）

§6.5 失败语义：中层 try/except → _log.debug → return None；
记忆是辅助（§4.3），主答绝不受影响。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 4: 写端到端 write→read 链路一致测试

- [ ] **Step 1: Write the failing test**

追加到 `tests/test_auth/test_memory_session.py`：

```python
@pytest.mark.asyncio
async def test_compact_then_read_returns_body_strip_frontmatter(tmp_path):
    """端到端：compact 写 + read_session_summary 读 链路一致（§6.7 场景 13）。

    write 后 read 拿到的就是 LLM 输出的 body（去 frontmatter + strip）。
    复制粘贴 prev_summary 的语义一致性回归。
    """
    fs = MemoryFS(root=str(tmp_path))
    msgs = _msgs(*[("user", f"q{i}") for i in range(6)])
    db = _FakeDB(msgs)
    llm = _FakeLLM(response="端到端摘要正文")
    compactor = SessionCompactor(llm)

    # 写
    await compactor.compact(fs, db, user_id=7, session_id="sess_e2e", every_n_messages=6)

    # 读
    body = await read_session_summary(fs, user_id=7, session_id="sess_e2e")

    # 链路一致：write 时 body=summary+"\n"，read 时 strip → 等于 LLM 输出原文
    assert body == "端到端摘要正文"
```

- [ ] **Step 2: Run test to verify pass**

Run: `./venv/bin/python -m pytest tests/test_auth/test_memory_session.py::test_compact_then_read_returns_body_strip_frontmatter -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_auth/test_memory_session.py
git commit -m "$(cat <<'EOF'
test(memory-s5): compact + read 端到端链路一致

write→read 拿到 LLM 输出原文（去 frontmatter + strip）；
保 prev_summary 在下轮递归中语义恒同（§6.4 递归累积不变量）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 5: T3 完整回归

- [ ] **Step 1: 运行 T3 全部测试**

Run: `./venv/bin/python -m pytest tests/test_auth/test_memory_session.py -v`
Expected: 6 (T1) + 7 (T2) + 4 (T3) = 17 passed

- [ ] **Step 2: T3 出口**

T3 共 4 个 commit（focus 持久化 / 跨租户隔离 / fs.write 失败隔离 / 端到端）；17 passed test 全绿；session.py 公开契约稳定。

---

## Task 4: 删除清单 + qa_router 接入 + 广回归

**Files:**
- Modify: `src/service/memory/service.py:14`（import 改）
- Modify: `src/service/memory/service.py:100-180`（删 maybe_compact_session）
- Modify: `src/service/db_models_homepage.py:360-388`（删 QASessionMemory 类）
- Modify: `src/service/qa_router.py:43-46`（删 maybe_compact_session import）
- Modify: `src/service/qa_router.py:269-307`（读侧 5b/5c 注入）
- Modify: `src/service/qa_router.py:596-602`（写侧 SessionCompactor 替换）
- Modify: `src/service/qa_engine/synthesizer.py:84,111`（注释清理）
- Modify: `src/service/memory/context_budget.py:4`（注释清理）
- Modify: `tests/test_auth/test_memory_service.py`（删 11 测试 + 改 import + 清 _FakeMemDB）
- Modify: `tests/test_auth/test_models_memory.py:7,43-69`（删 4 测试 + 改 import）
- Modify: `tests/test_auth/test_qa_router.py:419-422`（改 mock 点）

**Why this task:** T4 落 §6.2 删除清单 + §6.4 qa_router 读侧/写侧接入 + 注释清理 + 测试 obsolete 清理 + 广回归确保 **0 failed**。这是 S5 上线最后一步：S4 模式（先 src 改、再测试改、再广回归）。

### Step 1: 删 `maybe_compact_session` 函数 + 改 service.py import

- [ ] **Step 1: 改 `src/service/memory/service.py:14` import**

Edit `src/service/memory/service.py`：line 14 把
```python
from src.service.db_models_homepage import QASessionMemory, QAMessage
```
改为
```python
from src.service.db_models_homepage import QAMessage
```

- [ ] **Step 2: 删 `maybe_compact_session` 函数（line 100–180 整段）**

Edit `src/service/memory/service.py`：删除 line 99（空行）+ line 100 起的 `async def maybe_compact_session(...) -> None:` 整个函数体到 line 180（含末尾 `return`）。删完后 line 99 上一行应该是 `_extract_focus_entity_ids` 函数末尾（line 97 `return out`）。

- [ ] **Step 3: 验证 service.py import 检查**

Run: `./venv/bin/python -c "from src.service.memory.service import recall_memory_block, _extract_focus_entity_ids; print('OK')"`
Expected: `OK`

Run: `./venv/bin/python -c "from src.service.memory.service import maybe_compact_session"`
Expected: `ImportError: cannot import name 'maybe_compact_session'`

- [ ] **Step 4: Commit**

```bash
git add src/service/memory/service.py
git commit -m "$(cat <<'EOF'
refactor(memory-s5): 删 maybe_compact_session 函数 + QASessionMemory import

§6.2 删除清单：service.py:100-180 整段函数删除；line 14 import 仅留 QAMessage。
保留 _extract_focus_entity_ids（S4/S5 共用）+ recall_memory_block wrapper（S3 drop-in）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 2: 删 `QASessionMemory` ORM 类

- [ ] **Step 1: Edit `src/service/db_models_homepage.py`**

删除 line 360–388 整段（含 line 360 的 `# ─── 7. qa_session_memory ...` 注释 + line 363 `class QASessionMemory(Base):` 到 line 388 `nullable=False`，最后一个属性 `updated_at` 末尾闭括号）。删完后 line 360 应该是空行 + line 361 `# ─── 8. qa_project_memory（记忆系统 P2-S1 ...`。

注意：表名定义 `__tablename__ = "qa_session_memory"` 一并删除；SQL 表 stranded 留 S6 ops pass 写 Alembic migration drop。

- [ ] **Step 2: 验证 db_models_homepage.py import 检查**

Run: `./venv/bin/python -c "from src.service.db_models_homepage import QAMessage, QASession, QAUserMemory, QAProjectMemory; print('OK')"`
Expected: `OK`

Run: `./venv/bin/python -c "from src.service.db_models_homepage import QASessionMemory"`
Expected: `ImportError: cannot import name 'QASessionMemory'`

- [ ] **Step 3: Commit**

```bash
git add src/service/db_models_homepage.py
git commit -m "$(cat <<'EOF'
refactor(memory-s5): 删 QASessionMemory ORM 类

§6.2 删除清单 + Q2 拍板：删 ORM 类，SQL 表 stranded 留 S6 一起 Alembic
migration drop。无数据迁移：sessions 自然走文件路径（首压无 fs 记录 →
prev_turn_count=0 重压）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 3: 改 qa_router 写侧调用（SessionCompactor 替换 maybe_compact_session）

- [ ] **Step 1: 改 `src/service/qa_router.py:43–46` import 拆分**

Edit `src/service/qa_router.py`：把
```python
from src.service.memory.service import (
    recall_memory_block,
    maybe_compact_session,
)
```
改为
```python
from src.service.memory.service import recall_memory_block
```

- [ ] **Step 2: 替换 `qa_router.py:596–602` 写侧调用**

Edit `src/service/qa_router.py`：把现有
```python
        # 2. 会话压缩（§22 暂留 DB tier，S5 后续迁文件）
        try:
            await maybe_compact_session(
                db, llm, session_id=session_id, force=force_compact,
            )
        except Exception:
            pass
```
替换为
```python
        # 2. 会话压缩（S5：DB → 文件）
        # 局部 import 同 S4：保持模块顶部 import 轻量（启动期不拉 memory 模块
        # 链），post-turn 实际调用时延迟加载；与 service.recall_memory_block 同模式
        try:
            from src.service.memory.session import SessionCompactor
            # fs 复用 S4 块在 line 568 构造的 MemoryFS 实例（同闭包内同生命周期）；
            # 若 S4 块在 line 568 之前抛（仅 import 阶段可能，极罕见），fs 不存在
            # → 此处 try/except 中层捕获后 debug 静默退出（与 S5 §6.5 一致）
            compactor = SessionCompactor(llm)
            await compactor.compact(
                fs, db,
                user_id=user_id, session_id=session_id, force=force_compact,
            )
        except Exception:
            # 中层失败语义（§6.5）：debug 留痕 + 静默；compact 自身已含中层 try/except，
            # 此外层为深度防御冗余兜底（与 S3/S4 同模式）
            _log.debug(
                "S5 SessionCompactor failed for session %s, silently ignored",
                session_id, exc_info=True,
            )
```

- [ ] **Step 3: import 自检**

Run: `./venv/bin/python -c "from src.service import qa_router; print('OK')"`
Expected: `OK`（无 import 错）

- [ ] **Step 4: Commit**

```bash
git add src/service/qa_router.py
git commit -m "$(cat <<'EOF'
refactor(memory-s5): qa_router 写侧 maybe_compact_session → SessionCompactor

§6.4 写侧接入：闭包内构造 SessionCompactor(llm) + compact(fs, db, ...)；
fs 复用 S4 块在 line 568 构造的 MemoryFS 实例（同闭包内同生命周期）。
import 拆分：service 模块仅留 recall_memory_block（drop-in S3 wrapper）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 4: qa_router 读侧 composer 接入

- [ ] **Step 1: 在 `qa_router.py:283` 后追加 5b/5c 段**

Edit `src/service/qa_router.py`：在现有 line 283 `        memory_block = ""` 之后（即第 5 段 try/except 之后），插入新段：

```python
    # 5b. 会话级 summary 注入（S5 — §4.3 落实读侧 composer）
    # 与 5 段共用 MemoryFS()；若 5 段 fs 构造已失败，此处构造新 fs（防御性）
    try:
        from src.service.memory.session import read_session_summary
        from src.service.memory.vfs import MemoryFS as _MemFS
        session_block = await read_session_summary(
            _MemFS(),                                # 默认 root 由 KE_MEM_ROOT 派生
            user_id=user.id,
            session_id=session_id,
        )
    except Exception:
        session_block = ""

    # 5c. 拼装：session 在前（更近的工作状态），global 在后（稳定身份/偏好/style）
    if session_block and memory_block:
        memory_block = session_block + "\n\n" + memory_block
    elif session_block:
        memory_block = session_block
    # 否则 memory_block 维持现状（可能空 / 可能仅 global）

```

注意：line 273–283 的 5 段 try/except 已结束（`memory_block = ""`），5b 与之并列；保留 line 284 起的 6 段（context_budget）逻辑不变。`MemoryFS as _MemFS` 别名避与 5 段已有 `from ... import MemoryFS` 冲突（局部 import 同名冲突 Python 仍 OK，但用别名更清晰）。

- [ ] **Step 2: 清理注释 line 286**

把 line 286 现有
```python
    #    更早轮由 system 记忆块 working_summary+focus 顶替。失败退回原行为，不抛。
```
改为
```python
    #    更早轮由 system 记忆块 working_summary 顶替（S5 已实现读侧 composer，
    #    5b/5c 把当前 session summary 拼到 memory_block 头部）。失败退回原行为，不抛。
```

- [ ] **Step 3: import 自检**

Run: `./venv/bin/python -c "from src.service import qa_router; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add src/service/qa_router.py
git commit -m "$(cat <<'EOF'
feat(memory-s5): qa_router 读侧 composer 接入（5b/5c 段）

§6.4 读侧算法：read_session_summary 注入当前 session 的 summary.md
到 memory_block 头部；session 在前（更近的工作状态），global 在后（稳定事实）。
落实 §4.3 设计原意 — trim 后旧轮历史不再静默丢失。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 5: 清理 synthesizer + context_budget 的 orphan 注释

- [ ] **Step 1: Edit `src/service/qa_engine/synthesizer.py`**

把 line 84 现有
```python
        §20：带最近历史原文（旧轮由 memory_block 的 working_summary 覆盖）。"""
```
改为
```python
        §20：带最近历史原文（旧轮由 memory_block 头部的 session summary 顶替，
        S5 已落实读侧 composer 在 qa_router 5b/5c 段）。"""
```

把 line 111 现有
```python
        §20：带最近历史原文（旧轮由 memory_block 的 working_summary 覆盖）。"""
```
改为同样的更新文本（与 line 84 一致）。

- [ ] **Step 2: Edit `src/service/memory/context_budget.py`**

把 line 4 现有
```python
更早轮由 system 记忆块的 working_summary+focus 顶替（不在此模块，在 recall）。
```
改为
```python
更早轮由 system 记忆块头部的 session summary 顶替（S5 在 qa_router 5b/5c
读侧 composer 实现：read_session_summary 拼到 memory_block 头部）。
```

- [ ] **Step 3: Commit**

```bash
git add src/service/qa_engine/synthesizer.py src/service/memory/context_budget.py
git commit -m "$(cat <<'EOF'
docs(memory-s5): 注释清理 — orphan working_summary 注释改事实陈述

S5 读侧 composer 已落地（qa_router 5b/5c）→ 注释里"working_summary 顶替"
不再是 aspirational，更新为事实：session summary 拼到 memory_block 头部。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 6: 删除 obsolete 测试 — test_memory_service.py

- [ ] **Step 1: Edit `tests/test_auth/test_memory_service.py:9–12` import 拆分**

把现有
```python
from src.service.memory.service import (
    maybe_compact_session,
)
from src.service.db_models_homepage import QASessionMemory
```
改为
```python
from src.service.memory.service import _extract_focus_entity_ids
```

（其他 imports 不动；删 `maybe_compact_session` 和 `QASessionMemory` 两个名字 + 改 `_extract_focus_entity_ids` 为显式导入路径，让 line 239/251/264 测试仍能跑）。

注：原 `_extract_focus_entity_ids` 在 service.py 是私有 helper，测试当前是怎么用的？检查 line 239: `def test_extract_focus_dedup_and_order_and_cap():` — 这些测试可能通过 `from src.service.memory.service import _extract_focus_entity_ids` 调用。验证 grep。

- [ ] **Step 2: 验证 `_extract_focus_entity_ids` 在 test_memory_service.py 中的用法**

Run: `grep -n "_extract_focus_entity_ids" tests/test_auth/test_memory_service.py`
Expected: 至少 1 行 import + 多行测试用例引用

如果 grep 结果有 `from src.service.memory.service import _extract_focus_entity_ids`，则 Step 1 改 OK；否则改 import 块为更精确的形式（保留原 _extract_focus_entity_ids 路径），调整 Step 1 内容。

- [ ] **Step 3: 删 11 个 maybe_compact_session 相关测试**

逐个删除（保留行号附近上下文判定边界）：

| 行 | 测试函数名 | 备注 |
|---|---|---|
| 196 | `test_compact_skips_below_threshold` | floor 守卫 |
| 204 | `test_compact_creates_summary_when_threshold_reached` | 首压 |
| 216 | `test_compact_skips_when_no_new_messages_since_last` | delta 守卫 |
| 270 | `test_compact_persists_focus_entity_ids_new_row` | focus 新行 |
| 283 | `test_compact_updates_focus_entity_ids_existing_row` | focus 更新 |
| 297 | `test_compact_force_bypasses_n_floor` | force=True |
| 306 | `test_compact_force_still_skips_when_nothing_new` | force delta |
| 316 | `test_compact_non_force_unchanged_below_floor` | floor 重测 |
| 449 | `test_compact_first_time_no_prior_summary_segment` | 首压无 prev |
| 465 | `test_compact_recursive_folds_prior_summary_and_only_new_msgs` | 递归 |
| 493 | `test_compact_existing_fixed_fake_still_works_regression` | 既有 fake 回归 |

每个函数定义到下一个 `@pytest.mark.asyncio` 或下一个 `async def` 或 `def` 之前整段删（含前导空行 + decorator）。

**保留**：line 239 / 251 / 264 三个 `test_extract_focus_*` 测试（S4/S5 共用）。

- [ ] **Step 4: 清 `_FakeMemDB` 中 QASessionMemory 相关基础设施**

定位 `_FakeMemDB` 类定义（grep `class _FakeMemDB`）。检查其中：
- `session_rows` / `session_memory_rows` 类字段（用于 mock QASessionMemory 行返回）— 删
- `db.added` 列表中 QASessionMemory 判定逻辑（line 180 `if ent is QASessionMemory:`）— 删该分支
- 任何 `QASessionMemory(...)` 构造调用 — 删

具体改动需检查 `_FakeMemDB` 完整实现（参 `Read tests/test_auth/test_memory_service.py 100-200`）后由实施 subagent 精确删；保留 QAMessage 相关逻辑（_extract_focus_entity_ids 测试需要 QAMessage fake）。

- [ ] **Step 5: 改 import line 12 删 QASessionMemory**

Step 1 已含此项；这里是验证：grep `QASessionMemory` in test_memory_service.py 应返 0 行（删完后）。

Run: `grep -n "QASessionMemory" tests/test_auth/test_memory_service.py`
Expected: 0 行（删干净）

- [ ] **Step 6: 运行 test_memory_service.py 全部测试**

Run: `./venv/bin/python -m pytest tests/test_auth/test_memory_service.py -v`
Expected: 删 11 测试后净剩约 26 - 11 = 15 测试全过（具体数字取决于其他保留测试）

- [ ] **Step 7: Commit**

```bash
git add tests/test_auth/test_memory_service.py
git commit -m "$(cat <<'EOF'
test(memory-s5): 删 11 maybe_compact_session 测试 + 清 _FakeMemDB

§6.2 删除清单：S5 把 DB tier maybe_compact_session 迁文件后，
DB 路径测试已 obsolete；S5 等效场景由 test_memory_session.py 覆盖。
保留 _extract_focus_entity_ids 测试（S4/S5 共用）+ QAMessage 基础设施。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 7: 删除 obsolete 测试 — test_models_memory.py

- [ ] **Step 1: Edit `tests/test_auth/test_models_memory.py:7` import**

把现有
```python
from src.service.db_models_homepage import QAUserMemory, QASessionMemory
```
改为
```python
from src.service.db_models_homepage import QAUserMemory
```

- [ ] **Step 2: 删 4 个 QASessionMemory schema 测试（line 43–69）**

删除：
- line 43 `def test_session_memory_table_name():` + 函数体
- line 47 `def test_session_memory_columns():` + 函数体
- line 55 `def test_session_memory_session_id_unique():` + 函数体
- line 60 `def test_session_memory_has_session_cascade_fk():` + 函数体

删除范围：line 43 到 line 69 整段（4 函数 + 之间空行）。

- [ ] **Step 3: 验证**

Run: `grep -n "QASessionMemory" tests/test_auth/test_models_memory.py`
Expected: 0 行

Run: `./venv/bin/python -m pytest tests/test_auth/test_models_memory.py -v`
Expected: 删 4 测试后净剩测试全过

- [ ] **Step 4: Commit**

```bash
git add tests/test_auth/test_models_memory.py
git commit -m "$(cat <<'EOF'
test(memory-s5): 删 4 QASessionMemory schema 测试

ORM 类已在 S5 删（db_models_homepage.py:360-388）；
schema 测试失去测试对象。SQL 表 stranded 留 S6 一起 drop。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 8: 改 test_qa_router.py mock 点

- [ ] **Step 1: Edit `tests/test_auth/test_qa_router.py:419–422`**

把现有
```python
    monkeypatch.setattr(
        "src.service.qa_router.maybe_compact_session",
        AsyncMock(return_value=None),
    )
```
改为
```python
    # patch SessionCompactor 类本身：让 SessionCompactor(llm).compact(...) 整体 no-op
    # S5 闭包内 lazy import，patch 真正符号位置 src.service.memory.session
    class _NoopCompactor:
        def __init__(self, llm):
            pass

        async def compact(self, *args, **kw):
            return None

    monkeypatch.setattr(
        "src.service.memory.session.SessionCompactor",
        _NoopCompactor,
    )
    # 同时 patch read_session_summary 为 no-op（5b 读侧也用文件，SQLite 测试 DB
    # 与文件 fs 独立，理论上 read 不存在路径会返 ""，但显式 patch 避免依赖文件状态）
    async def _empty_read(*args, **kw):
        return ""
    monkeypatch.setattr(
        "src.service.memory.session.read_session_summary",
        _empty_read,
    )
```

并把 line 407 的注释
```python
    注：maybe_compact_session 被 patch 为 no-op，因为 SQLite 测试 DB 的 BigInteger
```
改为
```python
    注：SessionCompactor + read_session_summary 被 patch 为 no-op（S5 后），
    避 SQLite 测试 DB 与真 fs 路径耦合（本测试目的是验 router 压力块→meta 接线，
    不测压缩 / composer 本身；专项覆盖由 test_memory_session.py 提供）。
```

- [ ] **Step 2: 验证**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_router.py -k "history_trimmed" -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_auth/test_qa_router.py
git commit -m "$(cat <<'EOF'
test(memory-s5): qa_router mock 点改 SessionCompactor + read_session_summary

S5 后 maybe_compact_session 已删；mock 目标改 patch
src.service.memory.session.SessionCompactor (类整体替换) +
read_session_summary (返 "")。压缩/composer 本体由 test_memory_session.py 覆盖。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 9: 广回归

- [ ] **Step 1: S5 单元全跑**

Run: `./venv/bin/python -m pytest tests/test_auth/test_memory_session.py -v`
Expected: 17 passed

- [ ] **Step 2: S4/S5 联合**

Run: `./venv/bin/python -m pytest tests/test_auth/test_memory_extract.py tests/test_auth/test_memory_session.py -v`
Expected: 18 + 17 = 35 passed

- [ ] **Step 3: memory 子目录 sanity**

Run: `./venv/bin/python -m pytest tests/test_auth/test_memory_service.py tests/test_auth/test_memory_recall.py tests/test_auth/test_memory_vfs.py tests/test_auth/test_memory_memgen.py tests/test_auth/test_memory_extract.py tests/test_auth/test_memory_session.py tests/test_auth/test_memory_prompt.py tests/test_auth/test_models_memory.py -v 2>&1 | tail -10`
Expected: 全 passed（净增 = 17 新 - 11 service - 4 models = +2 from S4 baseline；具体数字看实际删后剩多少）

- [ ] **Step 4: 全套广回归**

Run: `./venv/bin/python -m pytest tests/test_auth -q 2>&1 | tail -10`
Expected: 0 failed（与 S4 finishing 563 passed 基线对比；S5 删 15 测试 + 加 17 测试 = net +2 → **565 passed** 或附近）

- [ ] **Step 5: import 自检**

```bash
./venv/bin/python -c "from src.service import qa_router; print('qa_router OK')"
./venv/bin/python -c "from src.service.memory.session import SessionCompactor, read_session_summary, _summary_uri; print('session OK')"
./venv/bin/python -c "from src.service.memory.service import recall_memory_block, _extract_focus_entity_ids; print('service OK')"
./venv/bin/python -c "from src.service.memory.service import maybe_compact_session" 2>&1 | grep -q "ImportError" && echo "maybe_compact_session 已删 ✓"
./venv/bin/python -c "from src.service.db_models_homepage import QASessionMemory" 2>&1 | grep -q "ImportError" && echo "QASessionMemory 已删 ✓"
```
Expected: 全行 PASS（前 3 OK，后 2 显示已删）

- [ ] **Step 6: §4.3 / §22 残留 grep 自检**

```bash
grep -rn "maybe_compact_session" src/ tests/  # 应仅在 docs 残留（如有）；src/ + tests/ 应 0 行
grep -rn "QASessionMemory" src/ tests/        # 同上
```
Expected: 0 行（或仅在 docs/superpowers/plans/ 历史 plan 残留）

- [ ] **Step 7: Commit**

```bash
git add -A  # 任何残留小改动
git commit --allow-empty -m "$(cat <<'EOF'
test(memory-s5): 广回归 0 failed + import 自检 + grep 残留扫描

S5 部署最终验证：
- tests/test_auth -q 全套 passed，net +2 from S4 baseline (565 vs 563)
- maybe_compact_session + QASessionMemory grep src/+tests/ 双零
- session.py 公开契约稳定（SessionCompactor + read_session_summary + _summary_uri）
- qa_router 读侧 5b/5c + 写侧 SessionCompactor 接入完成

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

T4 出口：T4 共 9 个 commit；广回归 0 failed；§4.3 设计原意完整落实。

---

## Self-Review

### 1. Spec coverage check

| §6 spec 节 | 实现 task |
|---|---|
| §6.0 七个根本分叉 | T1–T4 各任务承载相应 Q 决策 |
| §6.1 架构与组件边界 | T1 建模块（写侧）+ T4 接入 qa_router（读侧+写侧） |
| §6.2 组件结构 | T1 新建 session.py + T4 删除清单（service.py / db_models_homepage.py / qa_router.py / 注释） |
| §6.3 文件格式 / Frontmatter Schema | T2 compact step 8（写 frontmatter）+ T1 read 不读 frontmatter（取 body）+ T2 frontmatter 损坏自愈测试 |
| §6.4 数据流 / 算法细节 | T2 compact 8 步 + T1 read_session_summary + T4 qa_router 接入 |
| §6.5 失败语义（三层深度防御） | T2 compact 整体 try/except + T1 read try/except + T3 fs.write 失败隔离测试 + T4 qa_router 闭包外层 try |
| §6.6 范围边界 | 全 plan 范围严守（无 S6/S7 任务） |
| §6.7 测试策略 13 场景 | T1: 1+2 (URI), 11 (read 不存在), 12 (read YAML 损坏); T2: 1 (首压), 2 (递归), 3-6 (守卫四联), 7 (frontmatter 自愈); T3: 8 (focus), 9 (跨租户), 10 (失败隔离), 13 (端到端) ✓ 13 个全部 |
| §6.8 决策日志 | 各 task commit message 引用 |
| §6.9 对 S6/S7 的交接注记 | T4 step 2 commit message 提及（QASessionMemory SQL 表 stranded） |

✅ 全覆盖。

### 2. Placeholder scan

Run: `grep -nE "TBD|TODO|FIXME|fill in|see Task|placeholder|待定|implement later|add appropriate" docs/superpowers/plans/2026-05-21-file-memory-s5-session-fs.md`
Expected: 0 行（plan 自身无占位）

注：plan 中 T2 stub `raise NotImplementedError("compact() implementation lands in S5 T2")` 是有意为之（T1→T2 间的失败信号），不是占位。

### 3. Type consistency

| 类型 / 签名 | T1 定义 | T2 使用 | T3 使用 | T4 使用 |
|---|---|---|---|---|
| `_summary_uri(user_id: int, session_id: str) -> str` | ✓ Step 3 | ✓ compact step 3 | ✓ T3 跨租户测试 | ✓ qa_router 5b 间接（read_session_summary 内部） |
| `read_session_summary(fs, *, user_id, session_id) -> str` | ✓ Step 2 Step 3 | — | ✓ T3 端到端测试 | ✓ qa_router 5b |
| `SessionCompactor(llm).__init__` | ✓ Step 4 Step 3 | — | — | ✓ qa_router 596 |
| `SessionCompactor.compact(fs, db, *, user_id, session_id, every_n_messages=6, force=False) -> None` | T1 Step 4 stub | ✓ T2 Step 1 Step 4 完整实现 | ✓ T3 测试 | ✓ qa_router 596 |
| `_split_frontmatter(text) -> tuple[dict, str]` | ✓ S2 既有 | ✓ T2 compact step 3 | ✓ T3 多处测试断言 | — |
| `_render_frontmatter(meta, body) -> str` | — | ✓ T2 compact step 8 | ✓ T3 多处构造预置 | — |
| `_extract_focus_entity_ids(messages) -> list[str]` | ✓ T1 import | ✓ T2 compact step 7 | ✓ T3 focus 测试 | — |
| `_now_iso_z() -> str` | ✓ T1 from extract import | ✓ T2 compact step 8 | — | — |

✅ 类型一致。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-21-file-memory-s5-session-fs.md`. Two execution options:

**1. Subagent-Driven (recommended)** - 同 S1/S2/S3/S4 模式：每 task 派 fresh subagent，两阶段 review（spec 合规 + 代码质量），修正闭环，末尾整体 holistic review。

**2. Inline Execution** - 本会话内顺序执行 4 tasks，检查点 review。

Which approach?
