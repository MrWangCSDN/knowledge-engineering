# 文件式记忆重构 S6 — DB 残留清理 + qa_messages 文件化 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** drop 5 stranded DB 表（qa_user_memory + qa_project_memory + qa_session_memory + qa_messages + qa_feedback）并把 qa_messages 写入路径从 DB 切到文件 `ke://u/{uid}/session/{sid}/messages/{msg_id}.md`。

**Architecture:** 两层改动。代码层：①`src/service/memory/session.py` 扩 `_FsMessage` dataclass + `write_message_to_fs` + `read_messages_for_session`，`SessionCompactor.compact` 删 `db` 参数 + step 1 改 `fs.ls` 替换 DB `select QAMessage`；②`src/service/qa_router.py` `persist_messages` callback 改写 fs。DB 层：新 Alembic migration `s6_drop_memory_tables.py` `op.drop_table` × 5（先 qa_feedback FK 子，再父）。无数据迁移（用户拍板：现存只是测试数据；D3「存量订正」语义失效）；无 LLM identity 收敛；无 rollback。

**Tech Stack:** Python 3.12.13 / pytest / pytest-asyncio / SQLAlchemy ORM / Alembic / S1 `MemoryFS` (vfs.py) / S2 frontmatter helpers (memgen.py) / S4 `_now_iso_z` (extract.py reuse) / 既有 `_SESSION_COMPACT_SYSTEM` prompt 不动。

**Spec source:** Obsidian `/Users/java/obsidian/01 Engineering/knowledge-engineering/文件式记忆重构-设计.md` §7（§7.0–§7.11）。

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `src/service/memory/session.py` | Modify | 扩 `_FsMessage` + `write_message_to_fs` + `read_messages_for_session`；改 `SessionCompactor.compact` 签名 + step 1；删 obsolete imports |
| `src/service/qa_router.py` | Modify | 改 `persist_messages` callback 写 fs；改 `_make_memory_writer` 中 `compactor.compact` 调用删 `db` 参数；清 obsolete imports |
| `src/service/db_models_homepage.py` | Modify | 删 `QAMessage` / `QAFeedback` / `QAUserMemory` / `QAProjectMemory` 4 ORM 类 + `QASession.messages` relationship |
| `alembic/versions/s6_drop_memory_tables.py` | **Create** | Alembic migration `op.drop_table` × 5 |
| `tests/test_auth/test_memory_session.py` | Modify | 加 8 新测试 + 改 17 既有测试用 `fs.write` 替代 `_FakeDB` + 删 fake DB 基础设施 |
| `tests/test_auth/test_models_memory.py` | Modify | 删 4 ORM schema 测试 + 改 import |
| `tests/test_auth/test_qa_router.py` | Modify | mock 点改加 `write_message_to_fs` patch + persist_messages 测试改 fs 验证 |

---

## Task 1: session.py 扩 fs message helpers + SessionCompactor.compact 改造 + 测试改造

**Files:**
- Modify: `src/service/memory/session.py:15-100`（扩 helpers + 改 compact）
- Modify: `tests/test_auth/test_memory_session.py`（加 8 新测试 + 改 17 既有 + 删 fake DB 基础设施）

**Why this task:** T1 落 §7.3 `_FsMessage` dataclass + 两个 fs message helpers + §7.5 `SessionCompactor.compact` 签名演进。同步改造既有 17 测试（用 `fs.write` 代替 `_FakeDB`），加 8 新测试覆盖 §7.9 全部场景。**T1 不改 src/ 之外的 db_models / qa_router / Alembic**（留 T2）。

### Step 1: 加 datetime imports + 删 obsolete imports

- [ ] **Inner Step 1: Edit `src/service/memory/session.py:15-27` imports 块**

把现有
```python
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
```
改为
```python
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from src.service.memory.vfs import MemoryFS, MemoryNotFound
from src.service.memory.memgen import _render_frontmatter, _split_frontmatter
from src.service.memory.service import _extract_focus_entity_ids
from src.service.memory.extract import _now_iso_z          # S4 既有 helper，原样复用
from src.service.qa_engine.prompts import _SESSION_COMPACT_SYSTEM
```

**改动要点**：
- 删 `from typing import Any`（S6 后 compact 无 `db: Any` 参数，无其他 Any 使用）
- 删 `from sqlalchemy import select`（S6 后 compact 不再 select QAMessage）
- 删 `from src.service.db_models_homepage import QAMessage`（QAMessage ORM T2 删除；S5 后 SessionCompactor 还引用，S6 切 fs.ls 后不再引用）
- 加 `from dataclasses import dataclass`（`_FsMessage` 用）
- 加 `from datetime import datetime, timezone`（`_FsMessage.created_at` + ISO parsing）

- [ ] **Inner Step 2: 验证 import 自检（暂时会失败，因为 SessionCompactor.compact 仍 reference QAMessage / select）**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "from src.service.memory import session" 2>&1 | head -5`
Expected: 因为 `compact` 方法体仍引用 `select(QAMessage)`，可能 NameError on `select` 或类似。这是预期的——下一 step 会同步删除 compact 中的旧引用。

不 commit 此 step（imports 与 compact 改造在同一逻辑 unit，下一 step 一并 commit）。

### Step 2: 改 `SessionCompactor.compact` 签名 + step 1（fs.ls 代替 DB select）

- [ ] **Inner Step 1: Edit `src/service/memory/session.py` 的 `compact` 方法体**

定位 `class SessionCompactor` 内的 `compact` 方法（约在文件中段）。把现有方法体替换为：

```python
    async def compact(
        self,
        fs: MemoryFS,
        *,
        user_id: int,
        session_id: str,
        every_n_messages: int = 6,
        force: bool = False,
    ) -> None:
        """post-turn 触发的会话压缩。

        设计：[[文件式记忆重构-设计]] §7.5（S6：删 db 参数，step 1 改 fs.ls）。
        保留 §6.4 verbatim 8 步算法 + 守卫语义；仅替换 DB↔fs 数据源。

        S6 后 backward-incompatible 签名变更：删 `db` 参数（step 1 不再读 DB）。
        调用方（qa_router._make_memory_writer 闭包）同步改 `compactor.compact(fs, user_id=..., session_id=..., force=...)`。

        Args:
            fs: S1 文件存储层（duck-type async API）— 既读 messages 也写 summary
            user_id: KE Integer ≥1，租户 ID
            session_id: KE String(64)，业务会话串
            every_n_messages: floor 阈值；msg_count < N 时早退（§6.4 step 2）
            force: True 表示上下文压力触发（spec §18），降低 floor 至 2、min_delta 至 1
        """
        try:
            # ─── step 1: 拉本 session 全部 messages（S6 后：fs 而非 DB） ───
            # read_messages_for_session 按 created_at 升序返；目录不存在返 []
            messages = await read_messages_for_session(
                fs, user_id=user_id, session_id=session_id
            )
            msg_count = len(messages)

            # ─── step 2: floor 判定（与 S5 同：force 降门槛到 2） ───
            floor = 2 if force else every_n_messages
            if msg_count < floor:
                return

            # ─── step 3: 读旧 summary.md（不存在 → 首压） ───
            uri = _summary_uri(user_id, session_id)
            prev_turn_count = 0
            prev_summary = ""
            try:
                raw = await fs.read(uri)
                fm, body = _split_frontmatter(raw)
                prev_summary = (body or "").strip()
                tc = fm.get("turn_count")
                if isinstance(tc, int) and tc >= 0:
                    prev_turn_count = tc
            except MemoryNotFound:
                pass

            # ─── step 4: delta 判定 ───
            min_delta = 1 if force else every_n_messages
            if msg_count - prev_turn_count < min_delta:
                return

            # ─── step 5: 拼 convo（§21 递归累积） ───
            new_msgs = messages[prev_turn_count:]
            parts: list[str] = []
            if prev_summary:
                parts.append("【已有会话摘要】\n" + prev_summary)
            if new_msgs:
                parts.append(
                    "【新增对话】\n"
                    + "\n".join(
                        f"[{m.role}] {(m.content or '')[:200]}" for m in new_msgs
                    )
                )
            convo = "\n\n".join(parts)

            # ─── step 6: LLM 调用 ───
            summary = await self._llm.complete(system=_SESSION_COMPACT_SYSTEM, user=convo)
            summary = (summary or "").strip()
            if not summary:
                return

            # ─── step 7: focus_entity_ids 聚合 ───
            focus = _extract_focus_entity_ids(messages[-12:])

            # ─── step 8: 写新 summary.md ───
            fm_new = {
                "turn_count": msg_count,
                "focus_entity_ids": focus,
                "updated_at": _now_iso_z(),
            }
            content = _render_frontmatter(fm_new, summary + "\n")
            await fs.write(uri, content)

        except Exception:
            _log.debug(
                "SessionCompactor.compact failed for session %s, silently ignored",
                session_id, exc_info=True,
            )
            return
```

**关键改动**：
- 签名删 `db: Any` 参数
- step 1 替换：`await db.execute(select(QAMessage)...).scalars().all()` → `await read_messages_for_session(fs, user_id=..., session_id=...)`
- 其余 step 2-8 与 S5 verbatim 一致
- docstring 加 §7.5 设计引用 + backward-incompatible 注记

- [ ] **Inner Step 2: 验证 import 自检 + linter**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "from src.service.memory.session import SessionCompactor; print('OK')"`
Expected: FAIL — `NameError: name 'read_messages_for_session' is not defined`

Step 3 会加 `read_messages_for_session` 实现。

不 commit 此 step（同 step 1 imports 改动，与 step 3 helpers 一起 commit）。

### Step 3: 加 `_FsMessage` dataclass + `write_message_to_fs` + `read_messages_for_session`

- [ ] **Inner Step 1: 在 `src/service/memory/session.py` 文件末尾追加新 helpers**

在 `class SessionCompactor` 之后追加：

```python
# ─── S6: fs-back message helpers（§7.3）──────────────────────────────────────


@dataclass
class _FsMessage:
    """fs-back message 鸭子类型（duck-type 等价于已删的 QAMessage ORM）。

    设计：[[文件式记忆重构-设计]] §7.3。
    满足 SessionCompactor.compact step 5 + _extract_focus_entity_ids 的属性访问契约：
    - role / content / msg_metadata / created_at

    S6 后 `SessionCompactor.compact step 1` 读 fs 返 `list[_FsMessage]` 替代
    DB ORM rows；下游代码（step 5 / step 7）按属性访问对接 — 鸭子兼容。
    """
    role: str
    content: str | None
    msg_metadata: dict | None
    created_at: datetime


def _messages_dir_uri(user_id: int, session_id: str) -> str:
    """URI 派生：ke://u/{uid}/session/{sid}/messages（无尾 /）。"""
    # 与 _summary_uri 同模式；末段不含文件名 — 用于 fs.ls 目录扫描
    return f"ke://u/{user_id}/session/{session_id}/messages"


def _message_uri(user_id: int, session_id: str, msg_id: str) -> str:
    """URI 派生：ke://u/{uid}/session/{sid}/messages/{msg_id}.md。

    msg_id 复用 KE 既有 String(64)（如 "msg_xyz789"），跨 session 唯一。
    """
    # f-string 拼装；S1 path safety 校验由 fs.read/fs.write 保证
    return f"ke://u/{user_id}/session/{session_id}/messages/{msg_id}.md"


async def write_message_to_fs(
    fs: MemoryFS,
    *,
    user_id: int,
    session_id: str,
    msg_id: str,
    role: str,
    content: str | None,
    sections: list[dict] | None = None,
    msg_metadata: dict | None = None,
    created_at: datetime | None = None,
) -> None:
    """写一条 message 到 fs（per-message file）。

    设计：[[文件式记忆重构-设计]] §7.3 + §7.4。
    `qa_router persist_messages` callback 用此 helper 替换 DB add/commit。

    路径：ke://u/{uid}/session/{sid}/messages/{msg_id}.md
    frontmatter: role + created_at(ISO Z) + 可选 sections + 可选 msg_metadata
    body: content + "\n"（与 S5 SessionCompactor.compact step 8 同约定）

    Args:
        fs: S1 文件存储层
        user_id / session_id / msg_id: 路径派生
        role: "user" / "assistant"
        content: 文本内容（user 必有；assistant 可为 None — 6 段式在 sections）
        sections: assistant 才有的 6 段式结构化内容
        msg_metadata: assistant 才有的 entry_points / cited_entities / token_usage 等
        created_at: 不传则用当前 UTC 时间（datetime.now(timezone.utc)）
    """
    # 默认时间 = 当前 UTC（与 _now_iso_z 同时区源）；调用方可传入精确时间
    if created_at is None:
        created_at = datetime.now(timezone.utc)
    # 拼 frontmatter dict；可选字段仅在非 None 时入 frontmatter（保持 YAML 干净）
    fm: dict = {
        "role": role,
        # ISO 8601 Z 字符串（与 S4 _now_iso_z 同格式）；YAML safe_dump 原样保留
        "created_at": created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if created_at.tzinfo else created_at.isoformat(),
    }
    if sections is not None:
        fm["sections"] = sections
    if msg_metadata is not None:
        fm["msg_metadata"] = msg_metadata
    # body 末尾换行（S2 _render_frontmatter docstring 约定）；content 可能 None
    body = (content or "") + "\n"
    # _render_frontmatter 真签名 (meta, body) → "---\n{YAML}---\n{body}"
    raw = _render_frontmatter(fm, body)
    # 路径派生 + fs.write 原子（S1 os.replace POSIX rename）
    uri = _message_uri(user_id, session_id, msg_id)
    await fs.write(uri, raw)


async def read_messages_for_session(
    fs: MemoryFS, *, user_id: int, session_id: str,
) -> list[_FsMessage]:
    """读 session 全部 message 文件，按 created_at 升序返。

    设计：[[文件式记忆重构-设计]] §7.3。drop-in 替换 SessionCompactor.compact
    step 1 的 DB `select QAMessage`（S6 后唯一 message 真相源）。

    路径不存在（session 无消息）→ 返 []（首压路径 / 新 session）。
    单 message 文件损坏（_split_frontmatter 抛 / 缺字段）→ _log.debug 跳过该消息
    （与 S2 单文件失败隔离同模式）。
    """
    # 拼目录路径（不含尾 /）
    dir_uri = _messages_dir_uri(user_id, session_id)
    try:
        filenames = await fs.ls(dir_uri)
    except MemoryNotFound:
        # 目录不存在 = session 还没写过任何 message（首压前 / 新 session）→ 返空
        return []
    except Exception as exc:
        # ls 其他异常（路径越界 / 权限等）— 防御性 debug 静默
        _log.debug("read_messages_for_session ls failed: %r", exc)
        return []

    out: list[_FsMessage] = []
    for fname in filenames:
        # fs.ls 返排序后的条目；我们仍按 created_at 排序（不依赖字典序）
        if not fname.endswith(".md"):
            # 非 .md 文件（理论上不该有）— 跳过
            continue
        # 文件 URI 拼接（不复用 _message_uri 因为已知 fname，避免重新构造）
        file_uri = f"{dir_uri}/{fname}"
        try:
            raw = await fs.read(file_uri)
            fm, body = _split_frontmatter(raw)
            # role 必填；缺失 / 非字符串 → 跳过该消息
            role = fm.get("role")
            if not isinstance(role, str) or not role:
                _log.debug("read_messages_for_session: skip missing role in %s", file_uri)
                continue
            # created_at 必填；解析 ISO 8601 Z（Python 3.12 datetime.fromisoformat 原生支持 Z）
            created_at_str = fm.get("created_at")
            if not isinstance(created_at_str, str):
                _log.debug("read_messages_for_session: skip missing created_at in %s", file_uri)
                continue
            created_at = datetime.fromisoformat(created_at_str)
            # 可选字段；类型容错（YAML 解析可能产出非预期类型）
            msg_metadata = fm.get("msg_metadata")
            if msg_metadata is not None and not isinstance(msg_metadata, dict):
                msg_metadata = None
            # body 是 frontmatter 之后的文本；去尾换行
            content_text = body.strip() if isinstance(body, str) else None
            # content 可能空字符串 — 保持为 "" 而非 None（与 user msg 必有 content 语义一致）；
            # 仅当文件 body 缺失（理论上不该）才 None
            out.append(_FsMessage(
                role=role,
                content=content_text,
                msg_metadata=msg_metadata,
                created_at=created_at,
            ))
        except Exception as exc:
            # 单文件损坏（_split_frontmatter / fromisoformat / fs.read 抛）→ debug 跳过，
            # 与 S2 / S5 单文件失败隔离同模式
            _log.debug("read_messages_for_session: skip corrupt file %s: %r", file_uri, exc)
            continue

    # 按 created_at 升序排序（不依赖 fs.ls 字典序 — 文件名是 msg_id，无时间排序保证）
    out.sort(key=lambda m: m.created_at)
    return out
```

**关键约束**：
- `_FsMessage` 是 `_ private` dataclass（§7.11 注记 #2：不视为公开 contract）
- `write_message_to_fs` / `read_messages_for_session` 是公开 free function（同 S3 `recall_memory_block` wrapper 模式）
- `_messages_dir_uri` / `_message_uri` 是 `_` private helper（不导出，仅 helper 内部用）
- 单文件失败隔离：损坏 / 缺字段 → _log.debug 跳过，不影响其他文件
- 排序：按 `_FsMessage.created_at` 升序（确定性）

- [ ] **Inner Step 2: 验证 import + helper 可调用**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "from src.service.memory.session import SessionCompactor, read_session_summary, _summary_uri, write_message_to_fs, read_messages_for_session, _FsMessage; print('OK')"`
Expected: `OK`

- [ ] **Inner Step 3: Commit（step 1 imports + step 2 compact 改造 + step 3 helpers 一起）**

```bash
cd /Users/java/knowledge-engineering-auth && git add src/service/memory/session.py && git commit -m "$(cat <<'EOF'
feat(memory-s6): session.py 扩 fs message helpers + compact 删 db 参数

S6 T1 step 1-3 (§7.3 + §7.5)：
- imports: 删 sqlalchemy.select / QAMessage / Any；加 dataclass / datetime
- SessionCompactor.compact 签名删 db 参数（backward-incompatible 演进）
- compact step 1 改为 await read_messages_for_session(fs, ...) 替代 DB select
- 新 helpers: _FsMessage dataclass + _messages_dir_uri / _message_uri + write_message_to_fs + read_messages_for_session
- read 端：单文件损坏 _log.debug 跳过；按 created_at 升序排序

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 4: 写 `_FsMessage` + helpers 的单元测试（8 个新测试）

- [ ] **Inner Step 1: 在 `tests/test_auth/test_memory_session.py` 末尾追加 import 和测试**

定位文件末尾，在最后一个测试函数之后追加：

```python
# ─── S6 T1: fs message helpers 单元测试（§7.9 场景 1-6 + 端到端） ─────────────

from datetime import datetime, timezone, timedelta as _td
from src.service.memory.session import (
    _FsMessage, write_message_to_fs, read_messages_for_session,
    _messages_dir_uri, _message_uri,
)


@pytest.mark.asyncio
async def test_write_message_to_fs_user(tmp_path):
    """write_message_to_fs 写 user 消息：frontmatter.role=user + body=content + created_at ISO（§7.9 场景 1）。"""
    fs = MemoryFS(root=str(tmp_path))
    ts = datetime(2026, 5, 21, 10, 0, 0, tzinfo=timezone.utc)
    await write_message_to_fs(
        fs, user_id=7, session_id="sess_x", msg_id="msg_abc",
        role="user", content="帮我看 PaymentGateway", created_at=ts,
    )
    # 直接读 fs 验证
    raw = await fs.read("ke://u/7/session/sess_x/messages/msg_abc.md")
    from src.service.memory.memgen import _split_frontmatter
    fm, body = _split_frontmatter(raw)
    assert fm["role"] == "user"
    assert fm["created_at"] == "2026-05-21T10:00:00Z"
    # sections / msg_metadata 未传 → frontmatter 不含
    assert "sections" not in fm
    assert "msg_metadata" not in fm
    # body strip 后 = content
    assert body.strip() == "帮我看 PaymentGateway"


@pytest.mark.asyncio
async def test_write_message_to_fs_assistant_with_sections_and_metadata(tmp_path):
    """write_message_to_fs 写 assistant + sections + msg_metadata（§7.9 场景 2）。"""
    fs = MemoryFS(root=str(tmp_path))
    ts = datetime(2026, 5, 21, 10, 0, 5, tzinfo=timezone.utc)
    sections = [
        {"type": "overview", "title": "概览", "content": "正文", "references": []},
    ]
    metadata = {
        "entry_points": ["PaymentGateway.charge"],
        "cited_entities": ["method:PaymentGateway.retry"],
        "token_usage": 1234,
    }
    await write_message_to_fs(
        fs, user_id=7, session_id="sess_x", msg_id="msg_def",
        role="assistant", content=None,
        sections=sections, msg_metadata=metadata, created_at=ts,
    )
    raw = await fs.read("ke://u/7/session/sess_x/messages/msg_def.md")
    from src.service.memory.memgen import _split_frontmatter
    fm, body = _split_frontmatter(raw)
    assert fm["role"] == "assistant"
    assert fm["sections"] == sections
    assert fm["msg_metadata"] == metadata
    # content=None → body 仅含尾换行
    assert body.strip() == ""


@pytest.mark.asyncio
async def test_read_messages_for_session_dir_not_exists_returns_empty(tmp_path):
    """read_messages_for_session 目录不存在 → 返 []（§7.9 场景 3）。

    首压路径 / 新 session — 不应抛 MemoryNotFound 给调用方（SessionCompactor.compact step 1）。
    """
    fs = MemoryFS(root=str(tmp_path))
    result = await read_messages_for_session(fs, user_id=7, session_id="sess_new")
    assert result == []


@pytest.mark.asyncio
async def test_read_messages_for_session_sorts_by_created_at(tmp_path):
    """read_messages_for_session 多文件按 created_at 升序返（§7.9 场景 4）。

    不依赖 fs.ls 字典序：msg_b 文件名靠后但 created_at 更早 → 排在前。
    """
    fs = MemoryFS(root=str(tmp_path))
    t1 = datetime(2026, 5, 21, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 5, 21, 10, 0, 5, tzinfo=timezone.utc)
    # msg_b 文件名字典序在 msg_a 之后，但写入时间更早
    await write_message_to_fs(
        fs, user_id=7, session_id="sess_x", msg_id="msg_b",
        role="user", content="第一条", created_at=t1,
    )
    await write_message_to_fs(
        fs, user_id=7, session_id="sess_x", msg_id="msg_a",
        role="assistant", content="第二条", created_at=t2,
    )
    result = await read_messages_for_session(fs, user_id=7, session_id="sess_x")
    assert len(result) == 2
    # 按 created_at 升序：t1 先（"第一条"）→ t2 后（"第二条"）
    assert result[0].content == "第一条"
    assert result[1].content == "第二条"
    assert result[0].created_at < result[1].created_at


@pytest.mark.asyncio
async def test_read_messages_for_session_skips_corrupt_file(tmp_path):
    """read_messages_for_session 单文件损坏 → _log.debug 跳过，其他文件正常返（§7.9 场景 5）。"""
    fs = MemoryFS(root=str(tmp_path))
    # 1 个正常文件
    await write_message_to_fs(
        fs, user_id=7, session_id="sess_x", msg_id="msg_good",
        role="user", content="正常消息",
        created_at=datetime(2026, 5, 21, 10, 0, 0, tzinfo=timezone.utc),
    )
    # 1 个 frontmatter 损坏的文件（缺 role 字段）
    bad_raw = "---\ncreated_at: \"2026-05-21T11:00:00Z\"\n---\n损坏消息\n"
    await fs.write("ke://u/7/session/sess_x/messages/msg_bad.md", bad_raw)

    result = await read_messages_for_session(fs, user_id=7, session_id="sess_x")
    # 仅返正常那一条；损坏的被跳过
    assert len(result) == 1
    assert result[0].content == "正常消息"


def test_fs_message_duck_type_contract():
    """_FsMessage 鸭子契约：4 属性齐备 + created_at 是 datetime（§7.9 场景 6）。"""
    ts = datetime(2026, 5, 21, 10, 0, 0, tzinfo=timezone.utc)
    m = _FsMessage(
        role="assistant",
        content="正文",
        msg_metadata={"cited_entities": ["e1"]},
        created_at=ts,
    )
    assert m.role == "assistant"
    assert m.content == "正文"
    assert m.msg_metadata == {"cited_entities": ["e1"]}
    assert isinstance(m.created_at, datetime)
```

- [ ] **Inner Step 2: 验证 6 新测试通过**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_session.py -k "test_write_message_to_fs or test_read_messages_for_session or test_fs_message_duck_type" -v 2>&1 | tail -10`
Expected: 6 passed（2 write + 3 read + 1 duck-type contract）

- [ ] **Inner Step 3: Commit**

```bash
cd /Users/java/knowledge-engineering-auth && git add tests/test_auth/test_memory_session.py && git commit -m "$(cat <<'EOF'
test(memory-s6): fs message helpers 6 单元测试（write + read + duck-type）

§7.9 场景 1-6：write_message_to_fs (user / assistant+sections+metadata),
read_messages_for_session (dir 不存在 / 按 created_at 排序 / 损坏文件跳过),
_FsMessage 鸭子契约（role/content/msg_metadata/created_at）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 5: 加 SessionCompactor.compact 端到端 + 签名测试（2 个新测试）

- [ ] **Inner Step 1: 追加到 `tests/test_auth/test_memory_session.py`**

```python
@pytest.mark.asyncio
async def test_session_compactor_compact_with_fs_messages_end_to_end(tmp_path):
    """SessionCompactor.compact 改 fs source 端到端（§7.9 场景 7）。

    write 4 messages (2 user + 2 assistant) → compact → 验 fs summary.md：
    - frontmatter.turn_count == 4
    - LLM 输入含【新增对话】4 条
    """
    fs = MemoryFS(root=str(tmp_path))
    # 写 4 messages（按 created_at 升序）
    base = datetime(2026, 5, 21, 10, 0, 0, tzinfo=timezone.utc)
    pairs = [
        ("msg_u1", "user", "q1"),
        ("msg_a1", "assistant", "a1"),
        ("msg_u2", "user", "q2"),
        ("msg_a2", "assistant", "a2"),
    ]
    for i, (mid, role, c) in enumerate(pairs):
        await write_message_to_fs(
            fs, user_id=7, session_id="sess_e2e", msg_id=mid,
            role=role, content=c, created_at=base + _td(seconds=i),
        )
    llm = _FakeLLM(response="4 条对话浓缩")
    compactor = SessionCompactor(llm)

    # 新签名：不传 db；force=True 让 floor=2 / min_delta=1 触发（4 messages 够）
    await compactor.compact(
        fs, user_id=7, session_id="sess_e2e", every_n_messages=6, force=True,
    )

    # 验 summary.md
    uri = "ke://u/7/session/sess_e2e/summary.md"
    raw = await fs.read(uri)
    from src.service.memory.memgen import _split_frontmatter
    meta, body = _split_frontmatter(raw)
    assert meta["turn_count"] == 4
    assert "4 条对话浓缩" in body
    # LLM 输入含 4 条 messages
    assert len(llm.calls) == 1
    user_input = llm.calls[0]["user"]
    assert "【新增对话】" in user_input
    assert "[user] q1" in user_input
    assert "[assistant] a1" in user_input
    assert "[user] q2" in user_input
    assert "[assistant] a2" in user_input


@pytest.mark.asyncio
async def test_session_compactor_compact_signature_no_db(tmp_path):
    """SessionCompactor.compact 新签名不含 db 参数（§7.9 场景 8）。

    backward-incompatible 演进确认：调用 compactor.compact(fs, user_id=..., session_id=..., ...) 不报 TypeError。
    """
    fs = MemoryFS(root=str(tmp_path))
    llm = _FakeLLM(response="摘要")
    compactor = SessionCompactor(llm)
    # 不传 db 参数 — S6 后正确签名
    await compactor.compact(
        fs, user_id=7, session_id="sess_sig", every_n_messages=6, force=False,
    )
    # msg_count=0 < floor=6 → 早退，文件未写（验证签名能调通即可）
    assert not await fs.exists("ke://u/7/session/sess_sig/summary.md")
```

- [ ] **Inner Step 2: 验证 2 新测试通过**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_session.py -k "test_session_compactor_compact_with_fs_messages_end_to_end or test_session_compactor_compact_signature_no_db" -v 2>&1 | tail -5`
Expected: 2 passed

- [ ] **Inner Step 3: Commit**

```bash
cd /Users/java/knowledge-engineering-auth && git add tests/test_auth/test_memory_session.py && git commit -m "$(cat <<'EOF'
test(memory-s6): SessionCompactor.compact 端到端 + 签名 2 新测试

§7.9 场景 7-8：write 4 messages → compact → 验 fs summary.md (turn_count=4
+ LLM 输入含【新增对话】4 条)；新签名不含 db 参数能调通。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 6: 改造既有 17 测试（删 _FakeDB 用 fs.write 准备数据）

- [ ] **Inner Step 1: 删除 _FakeDB / _FakeMessage / _FakeMsgScalars / _FakeMsgResult / _msgs 基础设施**

定位 `tests/test_auth/test_memory_session.py` 中（grep 找位置）：
- `from dataclasses import dataclass` — 保留（_FsMessage import）
- `class _FakeMessage` — 整 class 删
- `class _FakeMsgScalars` — 整 class 删
- `class _FakeMsgResult` — 整 class 删
- `class _FakeDB` — 整 class 删
- `def _msgs(...)` — 整函数删

`_FakeLLM` 保留（仍用）。

- [ ] **Inner Step 2: 把每个用 `_FakeDB` 的既有测试改为用 `fs.write` 准备数据**

逐个测试改造。grep 找 `_FakeDB(` 的所有 usage（17 个测试中 14 个用 _FakeDB；3 个不读 messages 不需）。

**Pattern**：
```python
# 旧（T1-T3 既有）：
fs = MemoryFS(root=str(tmp_path))
db = _FakeDB(_msgs(*[("user", f"q{i}") for i in range(6)]))
llm = _FakeLLM(...)
compactor = SessionCompactor(llm)
await compactor.compact(fs, db, user_id=7, session_id="sess_x", every_n_messages=6)

# 新（S6）：
fs = MemoryFS(root=str(tmp_path))
base = datetime(2026, 5, 21, 10, 0, 0, tzinfo=timezone.utc)
for i in range(6):
    role = "user" if i % 2 == 0 else "assistant"
    await write_message_to_fs(
        fs, user_id=7, session_id="sess_x", msg_id=f"msg_{i:03d}",
        role=role, content=f"q{i}", created_at=base + _td(seconds=i),
    )
llm = _FakeLLM(...)
compactor = SessionCompactor(llm)
await compactor.compact(fs, user_id=7, session_id="sess_x", every_n_messages=6)
```

需改造的 17 既有测试（按 grep 顺序）：
1. `test_compact_first_time_writes_new_summary` — _FakeDB → fs.write 6 messages
2. `test_compact_recursive_folds_prior_summary_and_only_new_msgs` — _FakeDB → fs.write 12 messages
3. `test_compact_skips_below_floor` — _FakeDB → fs.write 5 messages
4. `test_compact_skips_when_no_new_messages_since_last` — _FakeDB → fs.write 10 messages
5. `test_compact_force_bypasses_n_floor` — _FakeDB → fs.write 2 messages
6. `test_compact_llm_returns_empty_skips_write` — _FakeDB → fs.write 6 messages
7. `test_compact_with_corrupt_frontmatter_self_heals` — _FakeDB → fs.write 6 messages
8. `test_compact_persists_focus_entity_ids` — _FakeDB → fs.write 6 messages（带 msg_metadata）
9. `test_compact_cross_tenant_isolation` — _FakeDB → fs.write 6 messages for user_id=1
10. `test_compact_fs_write_failure_silently_logged` — _FakeDB → fs.write 6 messages（注意 fs.write 在两处用：准备 + 被 monkeypatch 抛；调整测试设计）
11. `test_compact_then_read_returns_body_strip_frontmatter` — _FakeDB → fs.write 6 messages

剩余 6 测试不读 messages（如 `test_summary_uri_basic` / `test_read_session_summary_not_exists_returns_empty` 等），不需改。

**特殊处理 #10**：`test_compact_fs_write_failure_silently_logged` 用 monkeypatch 抛 fs.write 异常。改造后，写 messages 阶段 fs.write 必须正常工作；只有 compact 内部的 fs.write summary.md 才该抛。需在 monkeypatch 前写完 messages，然后才 patch。修订版：

```python
@pytest.mark.asyncio
async def test_compact_fs_write_failure_silently_logged(tmp_path, monkeypatch):
    fs = MemoryFS(root=str(tmp_path))
    # 准备 messages（在 monkeypatch 前，fs.write 仍正常工作）
    base = datetime(2026, 5, 21, 10, 0, 0, tzinfo=timezone.utc)
    for i in range(6):
        role = "user" if i % 2 == 0 else "assistant"
        await write_message_to_fs(
            fs, user_id=7, session_id="sess_x", msg_id=f"msg_{i:03d}",
            role=role, content=f"q{i}", created_at=base + _td(seconds=i),
        )
    # 现在 monkeypatch fs.write 抛 — 影响后续 compact step 8 写 summary.md
    async def _explode(*args):
        raise OSError("simulated disk error")
    monkeypatch.setattr(fs, "write", _explode)
    llm = _FakeLLM(response="摘要")
    compactor = SessionCompactor(llm)
    # 应静默兜底
    await compactor.compact(fs, user_id=7, session_id="sess_x", every_n_messages=6)
    assert len(llm.calls) == 1
```

- [ ] **Inner Step 3: 验证 17 既有测试全部通过**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_session.py -v 2>&1 | tail -10`
Expected: 6 (T1) + 7 (T2) + 4 (T3) + 8 (S6 new) = **25 passed**（17 既有 + 8 新）

如果某测试失败，定位具体错并修复（可能因为 fs.ls 排序、created_at 解析等细节）。

- [ ] **Inner Step 4: Commit**

```bash
cd /Users/java/knowledge-engineering-auth && git add tests/test_auth/test_memory_session.py && git commit -m "$(cat <<'EOF'
refactor(memory-s6): test_memory_session.py 改 17 既有测试用 fs.write 替代 _FakeDB

§7.9：S6 后 compact 不再读 DB。删 _FakeMessage/_FakeMsgScalars/_FakeMsgResult/
_FakeDB/_msgs 基础设施；既有 11 个用 _FakeDB 的测试改为 write_message_to_fs
准备数据。test_compact_fs_write_failure_silently_logged 特殊处理：先写 messages
再 monkeypatch fs.write 防"准备数据被抛断"。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 7: T1 整体回归

- [ ] **Inner Step 1: T1 单元全跑**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_session.py -v 2>&1 | tail -5`
Expected: 25 passed

- [ ] **Inner Step 2: S4/S5 联跑确认无副作用**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_extract.py tests/test_auth/test_memory_session.py -v 2>&1 | tail -5`
Expected: 18 (extract) + 25 (session) = 43 passed

T1 出口：T1 共 4 个 commit（imports+compact+helpers / 6 helpers test / 2 compact test / 17 既有改造）；25 passed test 全绿；session.py 公开 contract 演进（compact 删 db；加 _FsMessage / write_message_to_fs / read_messages_for_session）。

---

## Task 2: 删 4 ORM 类 + qa_router 改造 + Alembic migration + 广回归

**Files:**
- Modify: `src/service/db_models_homepage.py:243-247, 250-435`（删 QASession.messages relationship + 4 ORM 类）
- Modify: `src/service/qa_router.py:238-264`（persist_messages 写 fs）
- Modify: `src/service/qa_router.py:_make_memory_writer`（compact 删 db 参数）
- Create: `alembic/versions/s6_drop_memory_tables.py`
- Modify: `tests/test_auth/test_models_memory.py`（删 4 ORM schema 测试）
- Modify: `tests/test_auth/test_qa_router.py`（mock 点加 write_message_to_fs patch + persist_messages 测试改）

**Why this task:** T2 落 §7.1 + §7.4 + §7.6 全部 src/ + test/ 改动；最终广回归 0 failed。

### Step 1: 删 QASession.messages relationship

- [ ] **Inner Step 1: Edit `src/service/db_models_homepage.py:243-247`**

定位 `class QASession` 内的 `messages` 字段（grep `messages: Mapped\[list\["QAMessage"\]\]`），删除 line 243-247：

```python
    # 关系定义（仅 Python 端，方便代码里写 sess.messages 而不需要再查）
    messages: Mapped[list["QAMessage"]] = relationship(
        back_populates="messages",
        cascade="all, delete-orphan",
        order_by="QAMessage.created_at",
    )
```

整段删除。`QASession` 末尾应直接以 `__table_args__` 结尾，无 `messages` 关系。

- [ ] **Inner Step 2: 不 commit（与下一 step 删 QAMessage class 一起 commit）**

### Step 2: 删 QAMessage / QAFeedback / QAUserMemory / QAProjectMemory 4 ORM 类

- [ ] **Inner Step 1: Edit `src/service/db_models_homepage.py:250-435`**

删除：
- line 250-290 `# ─── 4. qa_messages ───` 注释头 + `class QAMessage(Base):` 整段（含末尾 `session: Mapped["QASession"] = relationship(back_populates="messages")` 行）
- line 293-317 `# ─── 5. qa_feedback ───` 注释头 + `class QAFeedback(Base):` 整段
- line 319-357 `# ─── 6. qa_user_memory ───` 注释头 + `class QAUserMemory(Base):` 整段
- line 360-435 `# ─── 8. qa_project_memory ───` 注释头 + `class QAProjectMemory(Base):` 整段（注释里 "8" 是因 S5 删了 7. qa_session_memory）

删完后 `class QASession` 之后应直接到文件末尾（如果还有其他类，保留它们）。

- [ ] **Inner Step 2: 验证 import 自检**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "
from src.service.db_models_homepage import QASession, Project, GitCredential, UserProjectAccess
print('OK: 保留类正常')
"
```
Expected: `OK: 保留类正常`

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "from src.service.db_models_homepage import QAMessage" 2>&1 | grep ImportError && echo "✓ QAMessage 已删"
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "from src.service.db_models_homepage import QAFeedback" 2>&1 | grep ImportError && echo "✓ QAFeedback 已删"
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "from src.service.db_models_homepage import QAUserMemory" 2>&1 | grep ImportError && echo "✓ QAUserMemory 已删"
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "from src.service.db_models_homepage import QAProjectMemory" 2>&1 | grep ImportError && echo "✓ QAProjectMemory 已删"
```
Expected: 4 行 `✓ 已删`

- [ ] **Inner Step 3: Commit**

```bash
cd /Users/java/knowledge-engineering-auth && git add src/service/db_models_homepage.py && git commit -m "$(cat <<'EOF'
refactor(memory-s6): 删 4 ORM 类 + QASession.messages relationship

§7.1 删除清单：QAMessage / QAFeedback / QAUserMemory / QAProjectMemory 整类删；
QASession.messages 反向关系一并清（forward string 引用已删类会 mapper 报错）。
保留 QASession（业务元数据非记忆层，不在 S6 scope）+ 其他业务 ORM 类。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 3: 改 qa_router.py persist_messages callback

- [ ] **Inner Step 1: Edit `src/service/qa_router.py:238-264`**

把现有 `persist_messages` callback 替换为：

```python
    async def persist_messages(
        question: str, sections: list[dict], metadata: dict
    ) -> None:
        """流完成后写 user 消息 + assistant 消息到 fs；同时更新 qa_session.message_count。

        S6 改造（§7.4）：DB qa_messages insert → fs per-message file。
        失败语义：fs.write 抛 → debug 静默（与 §4.3 一致，不影响主答）。
        """
        # 1. 生成 msg_id（复用 S5 既有方式）+ 当前 UTC 时间
        user_msg_id = "msg_" + uuid.uuid4().hex[:12]
        assistant_msg_id = "msg_" + uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc)

        # 2. 写 fs (per-message file)
        # 局部 import 同 S4/S5：保持模块顶部 import 轻量
        try:
            from src.service.memory.session import write_message_to_fs
            from src.service.memory.vfs import MemoryFS as _MemFS
            fs = _MemFS()
            await write_message_to_fs(
                fs, user_id=user.id, session_id=session_id,
                msg_id=user_msg_id, role="user", content=question, created_at=now,
            )
            await write_message_to_fs(
                fs, user_id=user.id, session_id=session_id,
                msg_id=assistant_msg_id, role="assistant", content=None,
                sections=sections, msg_metadata=metadata, created_at=now,
            )
        except Exception:
            # 中层失败语义（§7.7）：debug + 静默；用户已看 SSE 答案 → 主答完好
            _log.debug(
                "persist_messages fs write failed for session %s, silently ignored",
                session_id, exc_info=True,
            )

        # 3. 更新 qa_session.message_count（QASession 仍在 DB；不在 S6 scope）
        async with db.begin_nested() if db.in_transaction() else _noop_ctx():
            sess = await db.get(QASession, session_id)
            if sess is not None:
                sess.message_count = (sess.message_count or 0) + 2
        await db.commit()
```

- [ ] **Inner Step 2: 在 `src/service/qa_router.py` 顶部 import 块加 datetime（若不存在）**

定位顶部 import 块。grep `from datetime` — 如果已 import 则跳过；否则在 imports 块加：
```python
from datetime import datetime, timezone
```

并 grep 确认是否还需要 `QAMessage` import — 应不再需要（旧 QAMessage 实例化已删）；若有 `from src.service.db_models_homepage import ..., QAMessage, ...` 行，从 import 列表删 `QAMessage` 名字。

- [ ] **Inner Step 3: 验证 qa_router import 自检**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "from src.service import qa_router; print('OK')"`
Expected: `OK`

- [ ] **Inner Step 4: Commit**

```bash
cd /Users/java/knowledge-engineering-auth && git add src/service/qa_router.py && git commit -m "$(cat <<'EOF'
refactor(memory-s6): qa_router persist_messages 写 fs 替代 DB qa_messages

§7.4：删 QAMessage(...) 实例化 + db.add_all(...)；改为 write_message_to_fs × 2
(user + assistant)。保留 qa_session.message_count 更新（QASession 仍 DB）。
中层 try/except 兜底 fs.write 失败 → _log.debug 静默（§7.7）。
顶部 import 加 datetime + 删 QAMessage import（已无引用）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 4: 改 qa_router _make_memory_writer compact 调用（删 db 参数）

- [ ] **Inner Step 1: Edit `src/service/qa_router.py` 的 `_make_memory_writer` 内部 compact 调用**

定位 `compactor.compact(fs, db, ...)` 调用（约在 line 622-627）。改为：

```python
            compactor = SessionCompactor(llm)
            await compactor.compact(
                fs,
                user_id=user_id, session_id=session_id, force=force_compact,
            )
```

**改动**：删 `db,` 参数（与 S6 新签名一致）。

- [ ] **Inner Step 2: 验证 qa_router import 自检**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "from src.service import qa_router; print('OK')"`
Expected: `OK`

- [ ] **Inner Step 3: Commit**

```bash
cd /Users/java/knowledge-engineering-auth && git add src/service/qa_router.py && git commit -m "$(cat <<'EOF'
refactor(memory-s6): qa_router compact 调用删 db 参数

§7.5：SessionCompactor.compact 新签名不含 db；调用方同步改。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 5: 新建 Alembic migration drop 5 表

- [ ] **Inner Step 1: 新文件 `alembic/versions/s6_drop_memory_tables.py`**

```python
"""S6: 文件式记忆重构 — DB 残留清理（drop 5 stranded 表）

Revision ID: s6_drop_memory_tables
Revises: qa_project_memory_p2s1_v1
Create Date: 2026-05-22

设计：[[文件式记忆重构-设计]] §7.6。
S6 后：qa_user_memory / qa_project_memory / qa_session_memory / qa_messages /
qa_feedback 5 表 stranded（无 reader/writer），统一 drop。

QASession 保留（sessions 元数据仍在 DB；S6 不涉）。
Users / Projects / UserProjectAccess / RepoCredential 保留（业务表）。

测试数据被 alembic 抹去 — 接受（D3：迁移即清理；用户拍板：现存只是测试数据）。
"""
from alembic import op

# Alembic 版本标识
revision = "s6_drop_memory_tables"
down_revision = "qa_project_memory_p2s1_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 先 drop 子（qa_feedback FK → qa_messages），再 drop 父
    # qa_messages 和 qa_session_memory 都有 FK → qa_sessions ondelete=CASCADE，
    # 但 qa_sessions 不在 drop 列表，FK 由 alembic op.drop_table 自动 drop 即可
    op.drop_table("qa_feedback")
    op.drop_table("qa_messages")
    op.drop_table("qa_session_memory")
    op.drop_table("qa_user_memory")
    op.drop_table("qa_project_memory")


def downgrade() -> None:
    # 不支持 downgrade — 数据已被 drop（D3 单向接受）；
    # 测试数据已抹，downgrade 无意义；若真要还原 schema 仅作 schema 复活，
    # 需手工 op.create_table 加回（不复数据），非常规路径。
    raise NotImplementedError(
        "S6 不支持 downgrade — 数据已被 drop（设计 §7.6）；"
        "若需还原 schema 仅作 schema 复活（不复数据），请手工 op.create_table 加回。"
    )
```

- [ ] **Inner Step 2: 验证 alembic 文件语法 + heads**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "
import importlib.util
spec = importlib.util.spec_from_file_location('s6', 'alembic/versions/s6_drop_memory_tables.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
print('revision:', mod.revision)
print('down_revision:', mod.down_revision)
"`
Expected: `revision: s6_drop_memory_tables` + `down_revision: qa_project_memory_p2s1_v1`

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/alembic heads 2>&1 | head -3`
Expected: `s6_drop_memory_tables (head)`

- [ ] **Inner Step 3: Commit**

```bash
cd /Users/java/knowledge-engineering-auth && git add alembic/versions/s6_drop_memory_tables.py && git commit -m "$(cat <<'EOF'
feat(memory-s6): Alembic migration drop 5 stranded memory tables

§7.6：drop qa_feedback (FK 子) → qa_messages → qa_session_memory →
qa_user_memory → qa_project_memory。down_revision=qa_project_memory_p2s1_v1
（当前 head 验证）。downgrade() raise NotImplementedError（D3 单向）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 6: 删 test_models_memory.py 中 4 ORM schema 测试

- [ ] **Inner Step 1: Edit `tests/test_auth/test_models_memory.py`**

定位文件 import 行（line 1-10 附近），把
```python
from src.service.db_models_homepage import QAUserMemory
```
（S5 已只留 QAUserMemory；qa_project_memory 测试用别的 import 路径或同名）
改为去掉所有已删 ORM 名字，仅保留尚需的 ORM 类 imports。如果整文件都是 schema 测试，且全部 ORM 已删，整文件可删 — 但需先 grep 确认。

具体步骤：
1. grep `^def test_` 列出所有测试函数
2. grep 各测试用的 ORM 类（`QAUserMemory` / `QAProjectMemory` / `QAMessage` / `QAFeedback`）
3. 删与已删类相关的测试 + 删 imports

预期：test_models_memory.py 全部测试都关联已删 ORM 类（S5 已删 4 个 QASessionMemory 测试；S6 删 11 个剩余）→ 整文件可删（如果完全无保留测试）。

如有保留测试（绑其他业务 ORM 如 QASession），仅删 ORM 已删的测试函数 + 改 import。

- [ ] **Inner Step 2: 验证测试运行**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_models_memory.py -v 2>&1 | tail -5`
Expected: 0 failed（剩余测试通过；如果整文件删了则 ImportError → 已处理）

- [ ] **Inner Step 3: Commit**

```bash
cd /Users/java/knowledge-engineering-auth && git add tests/test_auth/test_models_memory.py && git commit -m "$(cat <<'EOF'
test(memory-s6): 删 ORM schema 测试 (QAMessage/QAFeedback/QAUserMemory/QAProjectMemory)

§7.9 测试清理：4 ORM 类已删 → schema 测试失去测试对象。S5 已删
QASessionMemory schema 测试（4 个）；S6 删剩余 11 个。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 7: 改 test_qa_router.py mock 点 + persist_messages 测试

- [ ] **Inner Step 1: 加 `write_message_to_fs` patch 到既有 mock 块**

定位 `tests/test_auth/test_qa_router.py` 的 history_trimmed 测试中现有 mock 块（S5 加的 _NoopCompactor + _empty_read）。在 monkeypatch.setattr 列表后追加：

```python
    # 同时 patch write_message_to_fs 为 no-op（避 fs write 副作用 in tests）
    async def _noop_write(*args, **kw):
        return None
    monkeypatch.setattr(
        "src.service.memory.session.write_message_to_fs",
        _noop_write,
    )
```

- [ ] **Inner Step 2: 改 `test_explain_persists_user_and_assistant_messages` 测试**

定位该测试（约 line 241 附近，名字 `test_explain_persists_user_and_assistant_messages`）。原断言用 SQL 查 qa_messages 表；S6 后改用 fs 验证：

```python
@pytest.mark.asyncio
async def test_explain_persists_user_and_assistant_messages(session_maker, seed_ready_project):
    """端到端：explain → persist_messages 写 fs 两文件（user + assistant）。

    S6 改造：原断言 query qa_messages 表 → 改 query fs path。
    """
    # ... 既有 setup（同 S5 残留版本） ...

    # 执行 SSE 流 ...

    # 断言：fs 路径下应有 2 个 message 文件
    from src.service.memory.vfs import MemoryFS
    fs = MemoryFS()
    files = await fs.ls(f"ke://u/{user_id}/session/{session_id}/messages")
    assert len(files) == 2
    # 进一步验证 role / content（可选）
    # ... 读 fs 验 frontmatter ...
```

如果该测试设计变化太大（fs 模式不通），可改为 _NoopWrite mock 而仅断言 mock 被调 2 次。控制器在实施时按 plan 实际状态决定。

- [ ] **Inner Step 3: 验证 test_qa_router 通过**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_qa_router.py -v 2>&1 | tail -10`
Expected: 0 failed

- [ ] **Inner Step 4: Commit**

```bash
cd /Users/java/knowledge-engineering-auth && git add tests/test_auth/test_qa_router.py && git commit -m "$(cat <<'EOF'
test(memory-s6): test_qa_router.py mock 加 write_message_to_fs + persist 测试改 fs

§7.9：S6 后 persist_messages 走 fs；history_trimmed 测试加 patch
write_message_to_fs 为 no-op 避副作用；persist 测试改用 fs.ls 验证
而非 DB query。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 8: 广回归

- [ ] **Inner Step 1: S5/S6 模块全跑**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_session.py tests/test_auth/test_memory_extract.py -v 2>&1 | tail -5`
Expected: 25 (session) + 18 (extract) = 43 passed

- [ ] **Inner Step 2: 全套广回归**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth -q 2>&1 | tail -5`
Expected: 0 failed（S5 baseline 565；S6 删测试数 + 加 8 新测试 + 改 17 既有 = 预期 ~555±）

- [ ] **Inner Step 3: import 自检**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "from src.service import qa_router; print('qa_router OK')"
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "from src.service.memory.session import SessionCompactor, read_session_summary, _summary_uri, _FsMessage, write_message_to_fs, read_messages_for_session; print('session OK')"
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "from src.service.db_models_homepage import QASession, Project; print('preserved ORM OK')"
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "from src.service.db_models_homepage import QAMessage" 2>&1 | grep ImportError && echo "✓ QAMessage 已删"
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "from src.service.db_models_homepage import QAFeedback" 2>&1 | grep ImportError && echo "✓ QAFeedback 已删"
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "from src.service.db_models_homepage import QAUserMemory" 2>&1 | grep ImportError && echo "✓ QAUserMemory 已删"
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "from src.service.db_models_homepage import QAProjectMemory" 2>&1 | grep ImportError && echo "✓ QAProjectMemory 已删"
```
Expected: 全行 PASS（前 3 OK，后 4 显示已删）

- [ ] **Inner Step 4: grep 残留**

```bash
cd /Users/java/knowledge-engineering-auth && grep -rn "QAMessage\b\|QAFeedback\b\|QAUserMemory\b\|QAProjectMemory\b" src/ tests/ | grep -v "docs/\|.pyc" | head -20
```
Expected: 0 行（或仅在 docs/superpowers/plans/ 历史 plan 残留，不影响 runtime）

- [ ] **Inner Step 5: alembic 状态**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/alembic heads 2>&1 | head -3`
Expected: `s6_drop_memory_tables (head)`

注：**不**实际跑 `alembic upgrade head`（涉及 DB 改写需用户拍板）；仅验证 migration 文件已识别为 head。

- [ ] **Inner Step 6: Commit**

```bash
cd /Users/java/knowledge-engineering-auth && git add -A && git commit --allow-empty -m "$(cat <<'EOF'
test(memory-s6): 广回归 0 failed + import 自检 + grep 残留扫描

S6 部署最终验证：
- tests/test_auth -q 全套 passed (S5 baseline 565 → S6 预期 ~555±)
- session.py 公开 contract 演进（compact 删 db / 加 _FsMessage / write_message_to_fs / read_messages_for_session）
- 4 ORM 类 grep src/+tests/ 零（QAMessage/QAFeedback/QAUserMemory/QAProjectMemory）
- alembic head = s6_drop_memory_tables（未实际 upgrade — 待用户拍板）

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

T2 出口：T2 共 8 个 commit；广回归 0 failed；§7 设计完整落实。

---

## Self-Review

### 1. Spec coverage check

| §7 spec 节 | 实现 task |
|---|---|
| §7.0 brainstorm forks | T1+T2 各任务承载相应决策（极简版） |
| §7.1 架构总览（两层改动 + 部署顺序） | T1 代码层（session.py） + T2 代码层（qa_router）+ DB 层（Alembic） |
| §7.2 文件格式 / Frontmatter Schema | T1 Step 3 `write_message_to_fs` 实现 + Step 4 验证测试 |
| §7.3 fs message 读写 helper | T1 Step 3 完整实现 |
| §7.4 qa_router persist_messages 改造 | T2 Step 3 完整实现 |
| §7.5 SessionCompactor.compact step 1 改造 | T1 Step 2 完整实现（含签名删 db） |
| §7.6 Alembic migration | T2 Step 5 完整实现 |
| §7.7 失败语义（三层防御） | T1 Step 3 helpers + T1 Step 2 compact + T2 Step 3 callback 各包 try/except |
| §7.8 范围边界 | 全 plan 范围严守 |
| §7.9 测试策略 8 场景 | T1 Step 4-5 (6+2 新测试) + T1 Step 6 (17 既有改造) + T2 Step 6/7 (obsolete tests) |
| §7.10 决策日志 | 各 task commit message 引用 |
| §7.11 对 S7 的交接注记 | T1/T2 commit messages 提及 |

✅ 全覆盖。

### 2. Placeholder scan

Plan 自身 grep `TBD|FIXME|fill in|implement later|add appropriate|see Task [0-9]`：应 0 行（plan 实际命令内有 grep 字面量，可能误命中，需排除）。

T2 Step 7 提到 "如果该测试设计变化太大（fs 模式不通），可改为 _NoopWrite mock" — 这是 implementer flexibility 不是 placeholder。

### 3. Type consistency

| 类型 / 签名 | T1 定义 | T2 使用 |
|---|---|---|
| `_FsMessage(role, content, msg_metadata, created_at)` | T1 Step 3 | T1 Step 6 (改造既有测试) |
| `write_message_to_fs(fs, *, user_id, session_id, msg_id, role, content, sections=None, msg_metadata=None, created_at=None) -> None` | T1 Step 3 | T2 Step 3 qa_router 调用 |
| `read_messages_for_session(fs, *, user_id, session_id) -> list[_FsMessage]` | T1 Step 3 | T1 Step 2 compact step 1 |
| `SessionCompactor.compact(self, fs, *, user_id, session_id, every_n_messages=6, force=False)` | T1 Step 2 | T2 Step 4 qa_router 调用 |
| `_render_frontmatter(meta, body) -> str` | S2 既有 | T1 Step 3 write_message_to_fs |
| `_split_frontmatter(text) -> tuple[dict, str]` | S2 既有 | T1 Step 3 read_messages_for_session |

✅ 类型一致。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-22-file-memory-s6-cleanup.md`. Two execution options:

**1. Subagent-Driven (recommended)** - 同 S1-S5 模式：每 task 派 fresh subagent，两阶段 review（spec 合规 + 代码质量），修正闭环，末尾整体 holistic review。

**2. Inline Execution** - 本会话内顺序执行 2 tasks，检查点 review。

Which approach?
