# 会话级 focus_entity_ids 抽取 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让会话级记忆除 `working_summary` 外，还记录"本次围绕哪些代码实体"——把 P1 留空的 `qa_session_memory.focus_entity_ids` 填上并在召回时呈现。

**Architecture:** 纯代码增量，**无迁移**（列 P1 已建）。压缩时（`maybe_compact_session`）顺带从 assistant 消息既有 `msg_metadata` 里聚合 `cited_entities` + `entry_points`（零额外 LLM 成本），去重截断后与 `working_summary` 同一次 upsert 落库；`recall_memory_block` 在会话块内 `working_summary` 之后追加一行聚焦实体。失败语义沿用现有 broad-except（记忆绝不破坏主答）。

**Tech Stack:** Python / SQLAlchemy 2.0 async / pytest（Fake DB/LLM，沿用 `tests/test_auth/test_memory_service.py` 既有夹具风格）

**Spec:** `/Users/java/obsidian/01 Engineering/knowledge-engineering/记忆系统-设计.md` §17（本增量决策）、§4.3（会话级）、§7（召回顺序）

**Repo:** `/Users/java/knowledge-engineering-auth`，分支 `release-0513`（沿用；无 worktree）。venv：`source venv/bin/activate`。逐任务提交（已获授权，沿用 P1）。

---

## 现状基线（已核对真实代码，2026-05-17）

- `src/service/memory/service.py`：
  - `recall_memory_block(db, *, user_id, session_id)`：先查 `QASessionMemory`，`sm.working_summary` 非空 → `parts.append("【本次会话工作状态】\n"+summary)`；再查 `QAUserMemory` active；`return "\n\n".join(parts)`。
  - `maybe_compact_session(db, llm, *, session_id, every_n_messages=6)`：`messages = (...).scalars().all()`；增量 `< N` return；`convo = "\n".join(f"[{m.role}] {(m.content or '')[:200]}" for m in messages[-12:])`；`summary = await llm.complete(...)`；upsert：`sm is None` → `db.add(QASessionMemory(session_id=, working_summary=summary, turn_count=msg_count))`，否则 `sm.working_summary=summary; sm.turn_count=msg_count`；`await db.commit()`；最外层 `except Exception: _log.debug(...); return`。
  - 模块已 `import logging`、`_log = logging.getLogger(__name__)`、`from sqlalchemy import select`、`from src.service.db_models_homepage import QAUserMemory, QASessionMemory, QAMessage`。
- `QAMessage`：Python 属性 `msg_metadata`（DB 列名 `metadata`，`Mapped[Optional[dict]]`）。assistant 消息的 `msg_metadata` 形如 `{"token_usage","cost_yuan","latency_ms","entry_points":[...],"cited_entities":[...]}`（来自 `sse_emitter._collect_cited_entities`）。user 消息无 metadata。
- `QASessionMemory.focus_entity_ids`：`Mapped[Optional[list]] = mapped_column(JSON, nullable=True)`，P1 未写过。
- 测试夹具（`tests/test_auth/test_memory_service.py`）：`_FakeMemDB`（`execute` 按 `stmt.column_descriptions[0]["entity"]` 分派；`add`/`commit`）、`_FakeMemLLM`、`_FakeMsg(role="user", content="问题")`（**无 msg_metadata 属性**）。`_FakeResult.scalars()` 返回自身、`.all()`/`.one_or_none()`。

---

## File Structure

| 文件 | 改动 |
|---|---|
| `src/service/memory/service.py` | 加纯函数 `_extract_focus_entity_ids`；`maybe_compact_session` 压缩时算 focus 并写入两个 upsert 分支；`recall_memory_block` 会话块追加聚焦实体行 |
| `tests/test_auth/test_memory_service.py` | 追加 focus 抽取 / 压缩持久化 / 召回呈现 测试；扩展 `_FakeMsg` 支持可选 `msg_metadata` |

---

## Task 1: `_extract_focus_entity_ids` 纯函数 + 接入 `maybe_compact_session`

**Files:**
- Modify: `src/service/memory/service.py`
- Test: `tests/test_auth/test_memory_service.py`（追加）

- [ ] **Step 1: 追加失败测试**（追加到 `tests/test_auth/test_memory_service.py` 末尾）

```python
# ───────── 会话级 focus_entity_ids 抽取（spec §17）─────────

from src.service.memory.service import _extract_focus_entity_ids


class _FakeMsgMeta:
    """带 msg_metadata 的 fake 消息（assistant 轮）。"""
    def __init__(self, role="assistant", content="答", msg_metadata=None):
        self.role = role
        self.content = content
        self.msg_metadata = msg_metadata


def test_extract_focus_dedup_and_order_and_cap():
    msgs = [
        _FakeMsgMeta(msg_metadata={"cited_entities": ["method://a", "class://b"]}),
        _FakeMsgMeta(msg_metadata={"cited_entities": ["method://a"],  # 重复 a
                                   "entry_points": ["method://c"]}),
        _FakeMsgMeta(msg_metadata={"cited_entities": [f"method://x{i}" for i in range(20)]}),
    ]
    out = _extract_focus_entity_ids(msgs)
    assert out[:3] == ["method://a", "class://b", "method://c"]  # 首见序、去重
    assert len(out) == 10                                        # 截上限 10


def test_extract_focus_defensive_on_missing_or_bad_metadata():
    # 无 msg_metadata 属性 / None / 非 dict / 字段非 list / user 消息 → 全部安全跳过
    class _Bare:  # 连 msg_metadata 属性都没有
        role = "user"; content = "q"
    msgs = [
        _Bare(),
        _FakeMsgMeta(role="user", content="q", msg_metadata=None),
        _FakeMsgMeta(msg_metadata="not-a-dict"),
        _FakeMsgMeta(msg_metadata={"cited_entities": "not-a-list"}),
        _FakeMsgMeta(msg_metadata={"entry_points": None}),
    ]
    assert _extract_focus_entity_ids(msgs) == []


def test_extract_focus_filters_empty_and_nonstr():
    msgs = [_FakeMsgMeta(msg_metadata={"cited_entities": ["method://ok", "", None, 123]})]
    assert _extract_focus_entity_ids(msgs) == ["method://ok"]
```

并扩展现有 `_FakeMsg`（在其 `__init__` 增加可选 `msg_metadata`，默认 `None`，不破坏既有用法）；找到 `tests/test_auth/test_memory_service.py` 中的：

```python
class _FakeMsg:
    def __init__(self, role="user", content="问题"):
        self.role = role
        self.content = content
```
改为：
```python
class _FakeMsg:
    def __init__(self, role="user", content="问题", msg_metadata=None):
        self.role = role
        self.content = content
        self.msg_metadata = msg_metadata
```

再追加压缩持久化测试：

```python
@pytest.mark.asyncio
async def test_compact_persists_focus_entity_ids_new_row():
    msgs = [_FakeMsg() for _ in range(5)] + [
        _FakeMsg(role="assistant", content="答",
                 msg_metadata={"cited_entities": ["method://pay", "table://orders"]}),
    ]
    db = _FakeMemDB(session_row=None, msg_rows=msgs)
    await maybe_compact_session(db, _FakeMemLLM(), session_id="s1", every_n_messages=6)
    row = [o for o in db.added if isinstance(o, QASessionMemory)][0]
    assert row.focus_entity_ids == ["method://pay", "table://orders"]
    assert row.working_summary  # 摘要照常


@pytest.mark.asyncio
async def test_compact_updates_focus_entity_ids_existing_row():
    sm = QASessionMemory(session_id="s1", working_summary="old",
                         turn_count=0, focus_entity_ids=["method://old"])
    msgs = [_FakeMsg() for _ in range(5)] + [
        _FakeMsg(role="assistant",
                 msg_metadata={"cited_entities": ["method://new"]}),
    ]
    db = _FakeMemDB(session_row=sm, msg_rows=msgs)
    await maybe_compact_session(db, _FakeMemLLM(), session_id="s1", every_n_messages=6)
    assert sm.focus_entity_ids == ["method://new"]
    assert db.committed is True
```

- [ ] **Step 2: 跑测试，确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_service.py -q`
Expected: FAIL —— `ImportError: cannot import name '_extract_focus_entity_ids'`（及后续 focus 断言失败）

- [ ] **Step 3: 实现 helper + 接入压缩**

在 `src/service/memory/service.py` 中，于 `maybe_compact_session` 定义**之前**加纯函数：

```python
_FOCUS_MAX = 10  # 聚焦实体上限，控 prompt 体积


def _extract_focus_entity_ids(messages: Any) -> list[str]:
    """从一组消息的 msg_metadata 聚合本次会话聚焦的 entity_id。

    来源：assistant 消息 msg_metadata 里的 cited_entities + entry_points
    （回答时已落库，复用零额外 LLM 成本）。按首见序去重，截 _FOCUS_MAX。
    全程防御：缺属性 / None / 非 dict / 字段非 list / 非字符串项 一律安全跳过。
    设计：[[记忆系统-设计]] §17。
    """
    seen: set[str] = set()
    out: list[str] = []
    for m in messages:
        meta = getattr(m, "msg_metadata", None)
        if not isinstance(meta, dict):
            continue
        for key in ("cited_entities", "entry_points"):
            vals = meta.get(key)
            if not isinstance(vals, list):
                continue
            for v in vals:
                if isinstance(v, str) and v and v not in seen:
                    seen.add(v)
                    out.append(v)
                    if len(out) >= _FOCUS_MAX:
                        return out
    return out
```

在 `maybe_compact_session` 内，`summary = (summary or "").strip()` 与 `if not summary: return` **之后**、upsert **之前**，加：

```python
        focus = _extract_focus_entity_ids(messages[-12:])
```

两个 upsert 分支都带上 `focus_entity_ids`：

- `sm is None` 分支：
```python
            db.add(
                QASessionMemory(
                    session_id=session_id,
                    working_summary=summary,
                    turn_count=msg_count,
                    focus_entity_ids=focus,
                )
            )
```
- `else` 分支（更新已有）：
```python
            sm.working_summary = summary
            sm.turn_count = msg_count
            sm.focus_entity_ids = focus
```

> 注：focus 计算在最外层 `try` 内；即便异常也走既有 `except Exception: _log.debug(...); return`，不影响主答（§4.3 不变量保持）。

- [ ] **Step 4: 跑测试，确认通过**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_service.py -q`
Expected: 全 passed（含 3 个抽取 + 2 个压缩持久化新测试；既有测试不回归——`_FakeMsg` 加默认参数向后兼容）

- [ ] **Step 5: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/memory/service.py tests/test_auth/test_memory_service.py
git commit -m "$(cat <<'EOF'
feat(memory): P2 会话级 focus_entity_ids 抽取——压缩时聚合 cited/entry 实体（TDD）

零额外 LLM 成本（复用 assistant msg_metadata）；与 working_summary 同 upsert；
防御性解析；失败沿用 broad-except 不破坏主答。设计 [[记忆系统-设计]] §17。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `recall_memory_block` 召回呈现聚焦实体

**Files:**
- Modify: `src/service/memory/service.py`
- Test: `tests/test_auth/test_memory_service.py`（追加）

- [ ] **Step 1: 追加失败测试**

```python
@pytest.mark.asyncio
async def test_recall_includes_focus_entities_in_session_block():
    sm = QASessionMemory(session_id="s1", working_summary="已确认瓶颈在网关",
                         turn_count=6, focus_entity_ids=["method://pay", "table://orders"])
    db = _FakeMemDB(user_rows=[], session_row=sm)
    block = await recall_memory_block(db, user_id=1, session_id="s1")
    assert "已确认瓶颈在网关" in block
    assert "【本次聚焦实体】" in block
    assert "method://pay" in block and "table://orders" in block
    # 聚焦行在会话块内、紧随工作状态（仍属会话级，排在用户级之前）
    assert block.index("已确认瓶颈在网关") < block.index("【本次聚焦实体】")


@pytest.mark.asyncio
async def test_recall_no_focus_line_when_empty_or_none():
    sm1 = QASessionMemory(session_id="s1", working_summary="x", turn_count=6,
                          focus_entity_ids=[])
    sm2 = QASessionMemory(session_id="s2", working_summary="y", turn_count=6,
                          focus_entity_ids=None)
    for sid, sm in (("s1", sm1), ("s2", sm2)):
        db = _FakeMemDB(user_rows=[], session_row=sm)
        block = await recall_memory_block(db, user_id=1, session_id=sid)
        assert "【本次聚焦实体】" not in block
```

- [ ] **Step 2: 跑测试，确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_service.py -q`
Expected: FAIL —— `test_recall_includes_focus_entities_in_session_block` 断言 `【本次聚焦实体】` 不在 block

- [ ] **Step 3: 实现召回呈现**

在 `recall_memory_block` 中，把会话块构造改为（替换原 `if sm is not None and (sm.working_summary or "").strip():` 那一段）：

```python
    if sm is not None and (sm.working_summary or "").strip():
        session_block = "【本次会话工作状态】\n" + sm.working_summary.strip()
        focus = sm.focus_entity_ids
        if isinstance(focus, list):
            ids = [x for x in focus if isinstance(x, str) and x]
            if ids:
                session_block += "\n【本次聚焦实体】" + ", ".join(ids)
        parts.append(session_block)
```

其余（用户级查询、`return "\n\n".join(parts)`）不变。

- [ ] **Step 4: 跑测试，确认通过**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_service.py -q`
Expected: 全 passed（含 2 个召回呈现新测试；既有召回测试 `test_recall_*` 不回归——无 focus 时不加行）

- [ ] **Step 5: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/memory/service.py tests/test_auth/test_memory_service.py
git commit -m "$(cat <<'EOF'
feat(memory): P2 会话级召回呈现聚焦实体——会话块追加【本次聚焦实体】行（TDD）

仍属会话级、紧随工作状态、排在用户级前（§7 顺序不变）。空/None 不加行。
设计 [[记忆系统-设计]] §17。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: 回归验证（无新文件，无新 commit）

- [ ] **Step 1: 记忆全套**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_models_memory.py tests/test_auth/test_memory_prompt.py tests/test_auth/test_memory_service.py tests/test_auth/test_memory_router_hook.py -q`
Expected: 全 passed（P1 的 33 + 本增量新增 7 = 40，全绿）

- [ ] **Step 2: import 自检**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -c "import src.service.memory.service, src.service.qa_router; print('import OK')"`
Expected: `import OK`

- [ ] **Step 3: QA 链路回归（确认零新破坏）**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/ -q -k "qa or models or prompt or memory or chitchat"`
Expected: 全 passed（基线为本会话清理后 274/0；本增量纯加法 + `_FakeMsg` 向后兼容，应仍 0 fail）

---

## Self-Review（实施者跑完过一遍）

- [ ] spec §17 决策逐条对照：来源=cited_entities+entry_points ✓；时机=压缩同窗口同 upsert ✓；去重首见序+上限 10 ✓；召回会话块内紧随 summary ✓；无迁移 ✓；失败沿用 broad-except ✓
- [ ] 占位扫描：每个 code step 均完整可粘贴，命令均有 Expected ✓
- [ ] 类型一致：`_extract_focus_entity_ids(messages)->list[str]`；`focus_entity_ids` JSON 存 `list[str]`；`recall` 防御 `isinstance(list)`/`isinstance(str)` 与抽取端一致 ✓
- [ ] 向后兼容：`_FakeMsg` 加默认 `msg_metadata=None`；无 focus 时召回不加行；既有 P1 测试不改 ✓
- [ ] YAGNI：只动 2 函数 + 测试；不碰工程级/grounding/毕业；无迁移 ✓

## Phase Definition of Done

- [ ] `test_memory_service.py` 新增 7 测试全绿，记忆全套 40 passed
- [ ] QA 链路回归仍 0 fail（不回归本会话已修绿的 274）
- [ ] import OK
- [ ] 2 个 feat commit 干净（Task1 抽取 / Task2 召回）
- [ ] 无迁移、无生产门控步骤（纯代码增量）
