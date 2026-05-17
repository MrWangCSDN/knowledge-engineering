# 会话上下文压力压缩 + 摘要顶替旧轮 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 会话级记忆从「固定每 N 条压缩」升级为「按模型上下文窗口 token 压力驱动」，并让 prompt 只带最近若干轮原文、更早轮由已注入的 `working_summary`+`focus` 顶替（对齐 Claude Code「窗口快满→压缩→无缝继续」）。

**Architecture:** 纯代码 + 1 配置项，**无迁移**。新增纯模块 `context_budget.py`（token 估算/预算/历史裁剪）。router（已有 recall 处）按预算裁 `body.history` 只留最近轮、算 `context_usage`，喂给 `stream_qa_answer`（注入 `meta`，前端画进度条）；`maybe_compact_session` 加 `force`（压力高时越过固定 N floor，Fork C 混合）。`build_user_prompt_with_history` 内部逻辑**不动**（只喂裁好的 history，最小热路径侵入）。失败一律退回原行为且不抛（§4.3 不变量）。

**Tech Stack:** Python / FastAPI / pytest（Fake DB/LLM + `monkeypatch` 改 env；沿用 `tests/test_auth/` 既有风格）

**Spec:** `/Users/java/obsidian/01 Engineering/knowledge-engineering/记忆系统-设计.md` §18（定稿决策；Fork D=顶替/A=粗算/B=可配默认128000/C=压力+floor混合/E=本增量）

**Repo:** `/Users/java/knowledge-engineering-auth`，分支 `release-0513`。venv：`source venv/bin/activate`。逐任务提交（沿用授权）。

---

## 现状基线（已核对真实代码 2026-05-17）

- `src/service/qa_engine/prompts.py::build_user_prompt_with_history(question,context,history)`：纯函数，`history[-10:]`+每条`[:200]`，**本计划不改它**。
- `src/service/qa_engine/synthesizer.py::_estimate_tokens(system,user,output)` = `(len 之和)/1.5` 粗算（仅供参考，不复用其函数，本计划在新模块自带估算）。
- `src/service/qa_engine/sse_emitter.py::stream_qa_answer(*, question, project_id, session_id, retriever, synthesizer, router=None, history=None, on_complete=None, on_title=None, memory_block=None, on_memory=None)`；函数内先建 `meta_payload = {"session_id","message_id","plan_steps"}`，随后条件加 `skill_id` 等，之后 `yield format_sse("meta", meta_payload)`（"# 1. meta"）。
- `src/service/qa_router.py`：约 271–278 行 recall try/except 得 `memory_block`；285 起 `return StreamingResponse(stream_qa_answer(question=body.question, ..., history=body.history, on_complete=persist_messages, on_title=_make_title_generator(...), memory_block=memory_block, on_memory=_make_memory_writer(db=, llm=, user_id=, session_id=, question=)))`。`body.history: Optional[list[dict]]`，`user`/`db`/`session_id`/`synthesizer`/`is_new_session` 在作用域内（已被现有代码使用）。
- `src/service/memory/service.py::maybe_compact_session(db, llm, *, session_id, every_n_messages=6)`：`messages=...scalars().all()`；`if msg_count < every_n_messages: return`；取 `sm`；`prev=(sm.turn_count or 0) if sm else 0`；`if msg_count - prev < every_n_messages: return`；…`focus=_extract_focus_entity_ids(messages[-12:])`；两分支 upsert；`await db.commit()`；最外层 `except Exception: _log.debug(...); return`。
- `src/service/qa_router.py::_make_memory_writer(*, db, llm, user_id, session_id, question)`：内 `_writer()`：try 显式写；try `await maybe_compact_session(db, llm, session_id=session_id)`。
- 配置惯例：`os.getenv`（见 `db.py` 的 `KE_DB_URL`）。无任何上下文窗口配置。

---

## File Structure

| 文件 | 改动 |
|---|---|
| `src/service/memory/context_budget.py` | 🆕 纯模块：`estimate_tokens` / `model_context_window` / `history_token_budget` / `trim_history_to_budget` |
| `src/service/memory/service.py` | `maybe_compact_session` 加 `force: bool=False`（Fork C） |
| `src/service/qa_engine/sse_emitter.py` | `stream_qa_answer` 加 `context_usage: dict|None=None` → 注入 `meta_payload` |
| `src/service/qa_router.py` | 进流前按预算裁 history + 算 `context_usage`；`stream_qa_answer(history=eff_history, context_usage=..., on_memory=_make_memory_writer(..., force_compact=history_trimmed))`；`_make_memory_writer` 加 `force_compact` 透传 `maybe_compact_session(force=)` |
| `tests/test_auth/test_context_budget.py` | 🆕 |
| `tests/test_auth/test_memory_service.py` | 追加 `force` 压缩测试 + stream_qa_answer meta context_usage 测试 |
| `tests/test_auth/test_memory_router_hook.py` | 追加 `_make_memory_writer(force_compact=True)` 透传测试 |

---

## Task 1: `context_budget.py` 纯模块

**Files:** Create `src/service/memory/context_budget.py`; Create `tests/test_auth/test_context_budget.py`

- [ ] **Step 1: 失败测试** — 新建 `tests/test_auth/test_context_budget.py`：

```python
"""会话上下文 token 预算 / 历史裁剪（spec §18）纯函数测试。"""
from src.service.memory.context_budget import (
    estimate_tokens, model_context_window, history_token_budget,
    trim_history_to_budget,
)


def test_estimate_tokens_ceil_and_empty():
    assert estimate_tokens("") == 0
    assert estimate_tokens("123") == 2          # ceil(3/1.5)=2
    assert estimate_tokens("a" * 150) == 100


def test_window_env_override(monkeypatch):
    monkeypatch.delenv("KE_MODEL_CONTEXT_WINDOW", raising=False)
    assert model_context_window() == 128000      # 保守默认
    monkeypatch.setenv("KE_MODEL_CONTEXT_WINDOW", "1000000")
    assert model_context_window() == 1000000
    monkeypatch.setenv("KE_MODEL_CONTEXT_WINDOW", "garbage")
    assert model_context_window() == 128000       # 非法回退默认
    monkeypatch.setenv("KE_MODEL_CONTEXT_WINDOW", "-5")
    assert model_context_window() == 128000       # 非正回退默认


def test_history_budget_is_window_minus_reserve(monkeypatch):
    monkeypatch.setenv("KE_MODEL_CONTEXT_WINDOW", "100000")
    monkeypatch.delenv("KE_CTX_RESERVE_PCT", raising=False)
    assert history_token_budget() == 55000        # 100000*(1-0.45)
    monkeypatch.setenv("KE_CTX_RESERVE_PCT", "0.5")
    assert history_token_budget() == 50000


def test_trim_keeps_recent_within_budget_drops_older():
    hist = [
        {"role": "user", "content": "x" * 150},      # est 100
        {"role": "assistant", "content": "y" * 150}, # est 100
        {"role": "user", "content": "z" * 75},       # est 50
    ]
    kept, used = trim_history_to_budget(hist, budget=160)
    assert [m["content"][:1] for m in kept] == ["y", "z"]  # 丢最早，保最近、保序
    assert used == 150


def test_trim_defensive_and_min_one_recent():
    assert trim_history_to_budget(None, 100) == ([], 0)
    assert trim_history_to_budget([], 100) == ([], 0)
    assert trim_history_to_budget("notlist", 100) == ([], 0)
    # 单条远超预算 → 仍至少保留最近 1 条（不返回空历史）
    kept, used = trim_history_to_budget([{"role": "user", "content": "q" * 3000}], 10)
    assert len(kept) == 1 and used > 10
```

- [ ] **Step 2: 跑，确认失败** — `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_context_budget.py -q` → FAIL `ModuleNotFoundError: ...context_budget`

- [ ] **Step 3: 实现** — 新建 `src/service/memory/context_budget.py`：

```python
"""会话上下文 token 预算 / 历史裁剪（spec §18）。纯函数，无 DB/IO。

对齐 Claude Code：按模型窗口 token 压力决定保留多少最近原文轮；
更早轮由 system 记忆块的 working_summary+focus 顶替（不在此模块，在 recall）。
"""
from __future__ import annotations

import math
import os

_DEFAULT_WINDOW = 128000          # 保守默认（Fork B；env KE_MODEL_CONTEXT_WINDOW 覆盖）
_DEFAULT_RESERVE_PCT = 0.45       # 预留 system+记忆块+KG context+本轮问题+回答


def estimate_tokens(text: str) -> int:
    """粗算：1 token ≈ 1.5 字符（中英混合），向上取整，保守偏大（Fork A）。"""
    if not text:
        return 0
    return math.ceil(len(text) / 1.5)


def model_context_window() -> int:
    """模型上下文窗口（token）。env `KE_MODEL_CONTEXT_WINDOW` 覆盖；
    缺失/非法/非正 → 保守默认 128000。"""
    raw = os.getenv("KE_MODEL_CONTEXT_WINDOW", "").strip()
    try:
        v = int(raw)
        if v > 0:
            return v
    except (TypeError, ValueError):
        pass
    return _DEFAULT_WINDOW


def _reserve_pct() -> float:
    raw = os.getenv("KE_CTX_RESERVE_PCT", "").strip()
    try:
        v = float(raw)
        if 0.0 < v < 1.0:
            return v
    except (TypeError, ValueError):
        pass
    return _DEFAULT_RESERVE_PCT


def history_token_budget() -> int:
    """留给『原文历史』的 token 预算 = 窗口 × (1 − 预留比例)。"""
    return int(model_context_window() * (1.0 - _reserve_pct()))


def trim_history_to_budget(
    history, budget: int
) -> tuple[list[dict], int]:
    """保留最近若干轮使累计估算 token ≤ budget；更早轮丢弃（由 summary 顶替）。

    返回 (保留的最近列表[保持原序], 估算token)。防御：history 非 list/空/budget≤0
    → ([],0)；即便最近 1 条已超预算，也至少保留它（绝不返回空历史）。
    """
    if not isinstance(history, list) or not history or budget <= 0:
        return ([], 0)
    kept_rev: list[dict] = []
    used = 0
    for m in reversed(history):
        if not isinstance(m, dict):
            continue
        t = estimate_tokens(str(m.get("content", "")))
        if kept_rev and used + t > budget:
            break
        kept_rev.append(m)
        used += t
    kept_rev.reverse()
    return (kept_rev, used)
```

- [ ] **Step 4: 跑，确认通过** — 同上命令 → 全 passed（5 测试）

- [ ] **Step 5: Commit**
```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/memory/context_budget.py tests/test_auth/test_context_budget.py
git commit -m "$(cat <<'EOF'
feat(memory): P2② context_budget 纯模块——token 估算/窗口/预算/历史裁剪（TDD）

Fork A 粗算+余量、Fork B 可配默认128000。设计 [[记忆系统-设计]] §18。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `maybe_compact_session` 加 `force`（Fork C 混合）

**Files:** Modify `src/service/memory/service.py`; Test: `tests/test_auth/test_memory_service.py`（追加）

- [ ] **Step 1: 失败测试** — 追加到 `tests/test_auth/test_memory_service.py` 末尾：

```python
@pytest.mark.asyncio
async def test_compact_force_bypasses_n_floor():
    # 压力触发：force=True 时即便仅 2 条、且 < every_n_messages 也压缩
    db = _FakeMemDB(session_row=None, msg_rows=[_FakeMsg(), _FakeMsg(role="assistant")])
    await maybe_compact_session(db, _FakeMemLLM(), session_id="s1",
                                every_n_messages=6, force=True)
    assert any(isinstance(o, QASessionMemory) for o in db.added)
    assert db.committed is True


@pytest.mark.asyncio
async def test_compact_force_still_skips_when_nothing_new():
    # force 也要有"自上次压缩起 ≥1 新增"才压（msg_count==turn_count → 无新增 → 跳过）
    sm = QASessionMemory(session_id="s1", working_summary="prev", turn_count=2)
    db = _FakeMemDB(session_row=sm, msg_rows=[_FakeMsg(), _FakeMsg(role="assistant")])
    await maybe_compact_session(db, _FakeMemLLM(), session_id="s1",
                                every_n_messages=6, force=True)
    assert db.committed is False
    assert db.added == []


@pytest.mark.asyncio
async def test_compact_non_force_unchanged_below_floor():
    # 不传 force（默认 False）：行为与改前一致——2 条 < 6 → 跳过
    db = _FakeMemDB(session_row=None, msg_rows=[_FakeMsg(), _FakeMsg()])
    await maybe_compact_session(db, _FakeMemLLM(), session_id="s1", every_n_messages=6)
    assert db.added == []
```

- [ ] **Step 2: 跑，确认失败** — `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_service.py -q` → FAIL（`maybe_compact_session() got an unexpected keyword argument 'force'`）

- [ ] **Step 3: 实现** — 在 `src/service/memory/service.py`，`maybe_compact_session` 签名与两处 floor 守卫改为：

签名：
```python
async def maybe_compact_session(
    db: Any, llm: Any, *, session_id: str, every_n_messages: int = 6,
    force: bool = False,
) -> None:
```
docstring 末尾追加一句：`force=True（上下文压力，spec §18）：越过固定 N floor，但仍要求自上次压缩起 ≥1 条新增。`

第一处守卫 `if msg_count < every_n_messages: return` 改为：
```python
        floor = 2 if force else every_n_messages
        if msg_count < floor:
            return
```
第二处守卫 `if msg_count - prev < every_n_messages: return` 改为：
```python
        min_delta = 1 if force else every_n_messages
        if msg_count - prev < min_delta:
            return
```
其余（取 messages、取 sm、focus、两分支 upsert、commit、broad-except）一律不动。

- [ ] **Step 4: 跑，确认通过** — 同上 → 全 passed（含 3 新；既有 force 默认 False 向后兼容，原 compact 测试不回归）

- [ ] **Step 5: Commit**
```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/memory/service.py tests/test_auth/test_memory_service.py
git commit -m "$(cat <<'EOF'
feat(memory): P2② maybe_compact_session 加 force——压力触发越过固定 N floor（Fork C，TDD）

force 默认 False 完全向后兼容；force 仍要求 ≥1 新增。设计 [[记忆系统-设计]] §18。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: 热路径接线（router 裁史+算 usage / sse_emitter meta / _make_memory_writer force）

**Files:** Modify `src/service/qa_engine/sse_emitter.py`, `src/service/qa_router.py`; Tests: 追加 `tests/test_auth/test_memory_service.py` + `tests/test_auth/test_memory_router_hook.py`

- [ ] **Step 1: 失败测试**

追加到 `tests/test_auth/test_memory_service.py` 末尾（沿用其中已存在的 `_SpySynth`/`_StubRetriever`/`stream_qa_answer` import）：
```python
@pytest.mark.asyncio
async def test_stream_meta_carries_context_usage():
    synth = _SpySynth()
    cu = {"used_tokens": 1234, "budget_tokens": 128000, "pct": 1.0,
          "history_trimmed": True}
    chunks = []
    async for ev in stream_qa_answer(
        question="q", project_id="p1", session_id="s1",
        retriever=_StubRetriever(), synthesizer=synth, router=None,
        context_usage=cu,
    ):
        chunks.append(ev)
    meta = [c for c in chunks if c.startswith("event: meta")][0]
    assert '"context_usage"' in meta and '"history_trimmed":true' in meta
    assert '"budget_tokens":128000' in meta


@pytest.mark.asyncio
async def test_stream_meta_no_context_usage_when_none():
    synth = _SpySynth()
    chunks = []
    async for ev in stream_qa_answer(
        question="q", project_id="p1", session_id="s1",
        retriever=_StubRetriever(), synthesizer=synth, router=None,
    ):
        chunks.append(ev)
    meta = [c for c in chunks if c.startswith("event: meta")][0]
    assert "context_usage" not in meta
```

追加到 `tests/test_auth/test_memory_router_hook.py` 末尾：
```python
@pytest.mark.asyncio
async def test_writer_force_compact_threads_to_maybe_compact():
    # force_compact=True → 即便仅 2 条（< 默认 6）也应压缩（验证透传到 maybe_compact_session(force=True)）
    db = _FakeDB(msg_rows=[_FakeMsg(), _FakeMsg(role="assistant")])
    writer = _make_memory_writer(
        db=db, llm=_FakeLLM(), user_id=3, session_id="s1",
        question="无触发词的普通问题", force_compact=True,
    )
    await writer()
    assert any(isinstance(o, QASessionMemory) for o in db.added)
```

- [ ] **Step 2: 跑，确认失败** — `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_service.py tests/test_auth/test_memory_router_hook.py -q` → FAIL（`stream_qa_answer() got an unexpected keyword argument 'context_usage'` 等）

- [ ] **Step 3a: `sse_emitter.py`** — `stream_qa_answer` 签名在 `on_memory: OnMemoryCallback | None = None,` 之后追加：
```python
    context_usage: dict | None = None,
```
docstring `Args:` 末尾加一行：
```
        context_usage: 可选；非空时并入 meta 事件（前端画上下文进度条，spec §18）。
```
在 `yield format_sse("meta", meta_payload)` 这一行**之前**插入：
```python
    if context_usage is not None:
        meta_payload["context_usage"] = context_usage
```

- [ ] **Step 3b: `qa_router.py::_make_memory_writer`** — 签名加 `force_compact: bool = False`：
```python
def _make_memory_writer(*, db, llm, user_id, session_id, question, force_compact: bool = False):
```
内部 `await maybe_compact_session(db, llm, session_id=session_id)` 改为：
```python
            await maybe_compact_session(db, llm, session_id=session_id, force=force_compact)
```

- [ ] **Step 3c: `qa_router.py` 进流前裁史** — 在 recall 的 `try/except`（约 271–278，得 `memory_block`）**之后**、`return StreamingResponse(` **之前**插入：
```python
    # 6. 会话上下文压力（spec §18）：按 token 预算裁 body.history 只留最近若干轮，
    #    更早轮由 system 记忆块 working_summary+focus 顶替。失败退回原行为，不抛。
    try:
        from src.service.memory.context_budget import (
            history_token_budget, trim_history_to_budget,
            estimate_tokens, model_context_window,
        )
        _budget = history_token_budget()
        eff_history, _hist_used = trim_history_to_budget(body.history, _budget)
        _raw_n = len(body.history) if isinstance(body.history, list) else 0
        history_trimmed = _raw_n > len(eff_history)
        _window = model_context_window()
        _used = estimate_tokens(memory_block) + estimate_tokens(body.question) + _hist_used
        context_usage = {
            "used_tokens": _used,
            "budget_tokens": _window,
            "pct": round(min(_used / _window, 1.0) * 100, 1) if _window else 0.0,
            "history_trimmed": history_trimmed,
        }
    except Exception:
        eff_history = body.history
        history_trimmed = False
        context_usage = None
```
然后把 `return StreamingResponse(stream_qa_answer(...))` 调用里：
- `history=body.history,` → `history=eff_history,`
- `memory_block=memory_block,` 之后增加一行 `context_usage=context_usage,`
- `on_memory=_make_memory_writer(db=db, llm=synthesizer.llm, user_id=user.id, session_id=session_id, question=body.question,)` → 末尾加 `force_compact=history_trimmed,`

> 注：原来注释 `# 6. 返回 SSE 流` 顺延为 `# 7. 返回 SSE 流`（可选，仅注释）。`build_user_prompt_with_history` 不动——它收到的是已裁好的 history。

- [ ] **Step 4: 跑，确认通过** — `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_service.py tests/test_auth/test_memory_router_hook.py -q` → 全 passed（新 3 + 既有全绿）

- [ ] **Step 5: 回归 sse/router 既有** — `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_qa_engine_e2e.py tests/test_auth/test_qa_session_title.py tests/test_auth/test_qa_router.py -q` → 与基线一致（`test_qa_router.py` 6 个为本会话已修绿，应仍全绿；其余绿）。若出现新红（基线绿→红）必须查修；纯加默认参数+失败退回，预期零回归。

- [ ] **Step 6: Commit**
```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/qa_engine/sse_emitter.py src/service/qa_router.py tests/test_auth/test_memory_service.py tests/test_auth/test_memory_router_hook.py
git commit -m "$(cat <<'EOF'
feat(memory): P2② 热路径接线——按 token 预算裁史顶替旧轮 + meta 进度 + 压力强制压缩（TDD）

router 进流前按窗口预算裁 body.history（更早轮由 summary 块顶替，Fork D）；
stream_qa_answer meta 带 context_usage 供前端进度条；history 被裁→force 压缩。
build_user_prompt_with_history 内部不动。失败全退回原行为。设计 [[记忆系统-设计]] §18。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: 回归验证（无新文件/commit）

- [ ] **Step 1: 记忆全套 + 新模块** — `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_context_budget.py tests/test_auth/test_models_memory.py tests/test_auth/test_memory_prompt.py tests/test_auth/test_memory_service.py tests/test_auth/test_memory_router_hook.py -q` → 全 passed（基线记忆 40 + 本增量新增：context_budget 5 + force 3 + meta 2 + writer-force 1 = 11 → 共 51）
- [ ] **Step 2: import 自检** — `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -c "import src.service.memory.context_budget, src.service.memory.service, src.service.qa_router, src.service.qa_engine.sse_emitter; print('import OK')"` → `import OK`
- [ ] **Step 3: QA 链路回归** — `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/ -q -k "qa or models or prompt or memory or chitchat or context_budget" -p no:warnings` → 0 fail（基线本会话 281/0 + 本增量新增 11 → 292/0）

---

## Self-Review（实施者过一遍）

- [ ] spec §18 决策逐条对照：Fork D 顶替=router 裁 body.history 只留最近轮、更早由 summary 块覆盖、不改 build_user_prompt_with_history ✓；A 粗算 len/1.5 ✓；B env `KE_MODEL_CONTEXT_WINDOW` 默认 128000 ✓；C `force` 越 floor 但留 ≥1 新增 + 非 force 不变 ✓；进度条=meta.context_usage ✓；失败退回原行为不抛 ✓；无迁移 ✓
- [ ] 占位扫描：每 code step 完整可粘贴、命令带 Expected ✓
- [ ] 类型一致：`trim_history_to_budget(history,budget)->(list[dict],int)`；`context_usage: dict|None` 贯穿 router→stream_qa_answer→meta；`force/force_compact: bool` 贯穿 router→_make_memory_writer→maybe_compact_session ✓
- [ ] 向后兼容：所有新参数默认（`force=False`/`context_usage=None`/`force_compact=False`）；router try 失败 `eff_history=body.history`；既有测试不改 ✓
- [ ] YAGNI：不做精确 tokenizer、不做 per-provider 探测、不改 prompt 内部、不加迁移/列 ✓

## Phase Definition of Done

- [ ] 新增 11 测试全绿；记忆+context_budget 全套 51 passed
- [ ] QA 链路 292/0（不回归本会话 281/0 基线）
- [ ] import OK
- [ ] 3 feat commit 干净（context_budget / force / 热路径接线）
- [ ] 无迁移、无生产门控（纯代码 + 1 env 配置项，默认值即安全）
