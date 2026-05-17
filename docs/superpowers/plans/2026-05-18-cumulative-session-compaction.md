# 会话压缩累积化（修早期事实几轮即丢）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 让会话压缩**递归累积**（输入=上一版摘要+自水位线以来新增消息）并用**忠实对话摘要 prompt**，使早期事实（如"最早喜欢哈密瓜"）永久滚动保留，不再几轮即丢。

**Architecture:** 两处最小改动，对齐 Claude Code「携带摘要再摘要」：(1) `maybe_compact_session` 的摘要器输入从裸 `messages[-12:]` 改为 `【已有会话摘要】prev_summary + 【新增对话】messages[prev:]`（`prev` 即既有水位线 `turn_count`，代码里已算出，直接复用）；(2) `_SESSION_COMPACT_SYSTEM` 从"工作状态"任务向重写为"忠实保留事实/偏好/时间线、不得丢弃既有摘要"的对话摘要。触发/水位线/失败语义/upsert/recall/§20/焦点抽取/迁移 全不变。

**Tech Stack:** Python / pytest（沿用 `tests/test_auth` 既有 `_FakeMemDB`/`_FakeMemLLM`/`_FakeMsg` + 捕获型 fake）

**Spec:** `/Users/java/obsidian/01 Engineering/knowledge-engineering/记忆系统-设计.md` §21（定稿）+ §4.3/§18/§20

**Repo:** `/Users/java/knowledge-engineering-auth`，分支 `release-0513`（沿用，无 worktree）。逐任务提交（已授权）。

---

## 现状基线（已核对真实代码 2026-05-18）

`src/service/memory/service.py` `maybe_compact_session`（行 196 起，完整体 verbatim）：
```python
async def maybe_compact_session(
    db: Any, llm: Any, *, session_id: str, every_n_messages: int = 6,
    force: bool = False,
) -> None:
    """会话级压缩：每「自上次压缩以来新增 ≥ every_n_messages 条消息」压缩一次。

    设计：[[记忆系统-设计]] §4.3（P1 固定 N 轮，N=6 条≈3 轮问答）。
    turn_count 记录上次压缩时的 message_count；用「增量 ≥ N」判定，
    而非「过阈值后每轮都压」——否则消息每轮 +2，过阈后每轮都会调 LLM（成本 bug）。
    任何异常都吞掉并 debug 记录（记忆是辅助，绝不影响主答）。
    force=True（上下文压力，spec §18）：越过固定 N floor，但仍要求自上次压缩起 ≥1 条新增。
    """
    try:
        msg_res = await db.execute(
            select(QAMessage)
            .where(QAMessage.session_id == session_id)
            .order_by(QAMessage.created_at)
        )
        messages = msg_res.scalars().all()
        msg_count = len(messages)
        floor = 2 if force else every_n_messages
        if msg_count < floor:
            return

        sm_res = await db.execute(
            select(QASessionMemory).where(QASessionMemory.session_id == session_id)
        )
        sm = sm_res.scalars().one_or_none()

        # 距上次压缩的新增量不足 N → 跳过（实现「每 N 条压一次」而非「过阈后每轮压」）
        prev = (sm.turn_count or 0) if sm is not None else 0
        min_delta = 1 if force else every_n_messages
        if msg_count - prev < min_delta:
            return

        convo = "\n".join(
            f"[{m.role}] {(m.content or '')[:200]}" for m in messages[-12:]
        )
        summary = await llm.complete(system=_SESSION_COMPACT_SYSTEM, user=convo)
        summary = (summary or "").strip()
        if not summary:
            return

        focus = _extract_focus_entity_ids(messages[-12:])

        if sm is None:
            db.add(
                QASessionMemory(
                    session_id=session_id,
                    working_summary=summary,
                    turn_count=msg_count,
                    focus_entity_ids=focus,
                )
            )
        else:
            sm.working_summary = summary
            sm.turn_count = msg_count
            sm.focus_entity_ids = focus
        await db.commit()
    except Exception:
        # 压缩失败绝不影响主流程（spec §4.3）；debug 留痕便于排查（不影响主答）
        _log.debug(
            "maybe_compact_session failed for session %s, silently ignored",
            session_id, exc_info=True,
        )
        return
```
`src/service/qa_engine/prompts.py` `_SESSION_COMPACT_SYSTEM`（当前 verbatim）：
```python
_SESSION_COMPACT_SYSTEM = (
    "你是会话工作状态压缩器。基于给定的多轮问答，用中文输出一段不超过 150 字的"
    "「当前工作状态」概括，只保留对后续追问有用的信息：本次会话目标、已确认的结论、"
    "已排除的方向、当前聚焦点。直接输出概括正文，不要前缀、不要解释、不要分点编号。"
)
```
测试基线 `tests/test_auth/test_memory_service.py`：`_FakeMemDB(user_rows=None, session_row=None, msg_rows=None, project_rows=None)`（`execute` 按 `stmt.column_descriptions[0]["entity"]` 分派：QAMessage→msg_rows，QASessionMemory→`[session_row]` 或 `[]`）；`_FakeMemLLM`（`async complete(*, system, user, **kw)` 返回固定串 `"本次目标：排查下单超时；已确认瓶颈在 PaymentGateway"`）；`_FakeMsg(role="user", content="问题", msg_metadata=None)`；`QASessionMemory` 已 import；`pytest`/`pytest.mark.asyncio`。`tests/test_auth/test_qa_prompts.py` 测 prompts 常量/函数。

---

## File Structure

| 文件 | 改动 |
|---|---|
| `src/service/qa_engine/prompts.py` | 整体重写 `_SESSION_COMPACT_SYSTEM` 常量（任务向→忠实对话摘要） |
| `src/service/memory/service.py` | `maybe_compact_session` 内：把 `convo = …messages[-12:]…` 一段换成递归累积输入（`prev_summary` + `messages[prev:]`）；`prev` 复用既有行 |
| `tests/test_auth/test_qa_prompts.py` | 追加 `_SESSION_COMPACT_SYSTEM` characterization 测试 |
| `tests/test_auth/test_memory_service.py` | 追加首次/二次压缩递归输入 测试（捕获型 fake LLM） |

---

## Task 1: 重写 `_SESSION_COMPACT_SYSTEM`（任务向 → 忠实对话摘要）

**Files:** Modify `src/service/qa_engine/prompts.py`; Test `tests/test_auth/test_qa_prompts.py`

- [ ] **Step 1: 失败测试** —— 追加到 `tests/test_auth/test_qa_prompts.py` 末尾：

```python
# ───────── §21：_SESSION_COMPACT_SYSTEM 忠实对话摘要 characterization ─────────
from src.service.qa_engine.prompts import _SESSION_COMPACT_SYSTEM


def test_session_compact_system_is_faithful_digest_not_taskonly():
    s = _SESSION_COMPACT_SYSTEM
    # 新取向：忠实保留事实/偏好/时间线、不得丢弃既有摘要
    assert "忠实保留" in s
    assert "不得丢弃" in s and "【已有会话摘要】" in s
    assert "时间线" in s
    assert "300" in s                       # 放宽到 ≤300 字
    # 旧任务向措辞不得残留
    assert "会话工作状态压缩器" not in s
    assert "本次会话目标" not in s
    assert "150" not in s
```

- [ ] **Step 2: 跑，确认失败** —— `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_qa_prompts.py -q`
  Expected: FAIL —— `assert "忠实保留" in s`（旧常量无此词）

- [ ] **Step 3: 实现** —— `src/service/qa_engine/prompts.py`，把整个 `_SESSION_COMPACT_SYSTEM = (...)` 常量替换为：

```python
_SESSION_COMPACT_SYSTEM = (
    "你是对话记忆压缩器。基于【已有会话摘要】（若有）与【新增对话】，输出一段"
    "更新后的会话摘要，忠实保留对后续有用的关键信息：用户陈述的事实与偏好"
    "及其先后/演变时间线、已确认的结论、当前状态、未决问题。"
    "不得丢弃【已有会话摘要】中的既有事实——把新信息融合进去，有变化则标注演变。"
    "不超过 300 字，中文，直接输出摘要正文，不要前缀、不要解释、不要分点编号。"
)
```
（仅替换该常量字符串内容；常量名 `_SESSION_COMPACT_SYSTEM`、位置、其余 prompt 常量/函数 一律不动。）

- [ ] **Step 4: 跑，确认通过** —— `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_qa_prompts.py -q`
  Expected: 全 passed（新 characterization + 既有 prompts 测试不回归——只改一个常量字符串）

- [ ] **Step 5: Commit**
```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/qa_engine/prompts.py tests/test_auth/test_qa_prompts.py
git commit -m "$(cat <<'EOF'
feat(memory): §21 _SESSION_COMPACT_SYSTEM 重写为忠实对话摘要（保留事实/偏好/时间线，TDD）

任务向"工作状态压缩器"→ 忠实保留用户陈述事实与演变时间线、不得丢弃【已有会话摘要】
既有事实；配合压缩累积化（§21）。设计 [[记忆系统-设计]] §21。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `maybe_compact_session` 递归累积输入

**Files:** Modify `src/service/memory/service.py`; Test `tests/test_auth/test_memory_service.py`

- [ ] **Step 1: 失败测试** —— 追加到 `tests/test_auth/test_memory_service.py` 末尾：

```python
# ───────── §21：会话压缩递归累积输入 ─────────

class _CapCompactLLM:
    """捕获 maybe_compact_session 喂给摘要器的 user 输入。"""
    def __init__(self, reply="更新后的摘要"):
        self._reply = reply
        self.last_user = None

    async def complete(self, *, system, user, **kw):
        self.last_user = user
        return self._reply


@pytest.mark.asyncio
async def test_compact_first_time_no_prior_summary_segment():
    # 首次压缩（sm=None）：输入只有【新增对话】，无【已有会话摘要】段
    msgs = [_FakeMsg(role="user", content="我喜欢吃哈密瓜")] + [
        _FakeMsg(role="assistant", content=f"a{i}") for i in range(5)
    ]
    llm = _CapCompactLLM()
    db = _FakeMemDB(session_row=None, msg_rows=msgs)
    await maybe_compact_session(db, llm, session_id="s1", every_n_messages=6)
    assert "【已有会话摘要】" not in llm.last_user
    assert "【新增对话】" in llm.last_user
    assert "我喜欢吃哈密瓜" in llm.last_user
    row = [o for o in db.added if isinstance(o, QASessionMemory)][0]
    assert row.working_summary == "更新后的摘要" and row.turn_count == 6


@pytest.mark.asyncio
async def test_compact_recursive_folds_prior_summary_and_only_new_msgs():
    # 二次压缩：sm 已有 working_summary（含"哈密瓜"）+ turn_count=4（水位线）
    # 10 条消息：前 4 条是"老原始消息"，messages[4:] 是 6 条新增；
    # 增量 6 == every_n_messages(6) → 正常触发，无需 force（与 force 语义解耦）
    sm = QASessionMemory(session_id="s1",
                          working_summary="用户最早喜欢哈密瓜", turn_count=4)
    msgs = [_FakeMsg(role="user", content=f"OLD-{i}") for i in range(4)] + [
        _FakeMsg(role="user", content="现在喜欢西瓜"),
        _FakeMsg(role="assistant", content="好的西瓜"),
        _FakeMsg(role="user", content="夏天到了"),
        _FakeMsg(role="assistant", content="确实"),
        _FakeMsg(role="user", content="再聊聊"),
        _FakeMsg(role="assistant", content="嗯"),
    ]
    llm = _CapCompactLLM()
    db = _FakeMemDB(session_row=sm, msg_rows=msgs)
    await maybe_compact_session(db, llm, session_id="s1", every_n_messages=6)
    u = llm.last_user
    # 递归：含【已有会话摘要】+ 旧摘要内容（哈密瓜经此被保留，非靠老原始消息）
    assert "【已有会话摘要】\n用户最早喜欢哈密瓜" in u
    # 【新增对话】只含 messages[prev=4:]，不重复老原始消息
    assert "【新增对话】" in u
    assert "现在喜欢西瓜" in u and "夏天到了" in u
    assert "OLD-0" not in u and "OLD-3" not in u
    assert sm.working_summary == "更新后的摘要" and sm.turn_count == 10


@pytest.mark.asyncio
async def test_compact_existing_fixed_fake_still_works_regression():
    # 既有风格 fake（固定返回、与输入无关）仍正常 upsert（不回归）
    sm = QASessionMemory(session_id="s1", working_summary="old", turn_count=0)
    msgs = [_FakeMsg() for _ in range(6)]
    db = _FakeMemDB(session_row=sm, msg_rows=msgs)
    await maybe_compact_session(db, _FakeMemLLM(), session_id="s1",
                                every_n_messages=6)
    assert sm.working_summary  # 被更新
    assert sm.turn_count == 6
    assert db.committed is True
```

- [ ] **Step 2: 跑，确认失败** —— `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_service.py -q`
  Expected: FAIL —— `test_compact_first_time_no_prior_summary_segment` 失败（当前 `convo` 无「【新增对话】」标记）

- [ ] **Step 3: 实现** —— `src/service/memory/service.py` `maybe_compact_session` 内，把这一段：
```python
        convo = "\n".join(
            f"[{m.role}] {(m.content or '')[:200]}" for m in messages[-12:]
        )
        summary = await llm.complete(system=_SESSION_COMPACT_SYSTEM, user=convo)
```
替换为（`prev` 复用上方既有的 `prev = (sm.turn_count or 0) if sm is not None else 0`）：
```python
        # §21 递归累积：输入 = 上一版摘要 + 仅自水位线(prev)以来的新增消息。
        # 早期事实进早期摘要后被永久滚动保留（对齐 Claude Code 携带摘要再摘要）；
        # 输入恒有界（旧摘要 ≤ 摘要上限 + 新增量有界），无 token 膨胀。
        prev_summary = (sm.working_summary or "").strip() if sm is not None else ""
        new_msgs = messages[prev:]
        parts: list[str] = []
        if prev_summary:
            parts.append("【已有会话摘要】\n" + prev_summary)
        # 守卫已保证 msg_count - prev >= min_delta >= 1 → new_msgs 必非空；
        # 仍以 if 守一层，与 prev_summary 段对称且对未来阈值改动稳健（记忆辅助路径绝不抛）。
        if new_msgs:
            parts.append(
                "【新增对话】\n"
                + "\n".join(
                    f"[{m.role}] {(m.content or '')[:200]}" for m in new_msgs
                )
            )
        convo = "\n\n".join(parts)
        summary = await llm.complete(system=_SESSION_COMPACT_SYSTEM, user=convo)
```
不动：函数签名、`floor`/`min_delta`/`force` 守卫、`prev` 那行本身、`if not summary: return`、`focus = _extract_focus_entity_ids(messages[-12:])`、两个 upsert 分支（`turn_count=msg_count`/`sm.turn_count=msg_count`）、最外层 `try/except + _log.debug`。

> 边界自证：到达此处时守卫已保证 `msg_count >= floor` 且 `msg_count - prev >= min_delta >= 1` → `new_msgs = messages[prev:]` 必非空 → `_segs` 至少一段 → `convo` 非空（首次 sm=None：prev=0、prev_summary="" → 仅【新增对话】=全部消息；二次：含【已有会话摘要】+仅新增）。`if not summary` 仍兜 LLM 空返回。

- [ ] **Step 4: 跑，确认通过** —— `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_service.py -q`
  Expected: 全 passed（3 新 + 既有 compact/focus/memory 测试不回归——固定返回 fake 与输入无关，断言的是 upsert/committed/focus 不是 convo）

- [ ] **Step 5: Commit**
```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/memory/service.py tests/test_auth/test_memory_service.py
git commit -m "$(cat <<'EOF'
fix(memory): §21 会话压缩递归累积——输入=上一版摘要+自水位线新增消息（修早期事实几轮即丢，TDD）

maybe_compact_session 不再对裸 messages[-12:] 重摘要；改 prev_summary(折入)+
messages[prev:](仅新增)，对齐 Claude Code 携带摘要再摘要 → 哈密瓜等早期事实
永久滚动保留。触发/水位线/upsert/失败语义/focus/迁移 全不变。设计 §21。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: 回归验证（无新文件/commit）

- [ ] **Step 1: 记忆+prompts 全套** —— `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_service.py tests/test_auth/test_qa_prompts.py tests/test_auth/test_qa_synthesizer.py tests/test_auth/test_qa_router_chitchat.py tests/test_auth/test_sse_emitter.py tests/test_auth/test_context_budget.py -q`
  Expected: 全 passed（新增 6；既有 compact/focus/chit-chat/prompt 不回归）

- [ ] **Step 2: import 自检** —— `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -c "import src.service.memory.service, src.service.qa_engine.prompts; from src.service.qa_engine.prompts import _SESSION_COMPACT_SYSTEM; print('OK', len(_SESSION_COMPACT_SYSTEM))"`
  Expected: `OK <一个 >50 的整数>`

- [ ] **Step 3: QA 链路广回归** —— `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/ -q -k "qa or sse or memory or prompt or chitchat or context_budget" -p no:warnings`
  Expected: 0 fail（本增量纯输入构造+prompt 文本，固定返回 fake 与输入无关，应零回归）

> 端到端（控制器 Preview MCP，不在 subagent 范围）：跑着的产品新对话「我喜欢吃哈密瓜」→「20岁喜欢葡萄」→「30岁喜欢西瓜」→ 多轮后问「我最开始喜欢吃什么水果」应答「哈密瓜」（不再丢）；截图留证。

---

## Self-Review（实施者过一遍）

- [ ] spec §21 逐条：`maybe_compact_session` 输入=`prev_summary`+`messages[prev:]` 递归累积 ✓(T2)；`prev` 复用既有水位线行 ✓(T2)；`_SESSION_COMPACT_SYSTEM` 忠实对话摘要重写 ✓(T1)；触发/水位线/upsert/失败语义/focus(`messages[-12:]`)/recall/§20/迁移 全不变 ✓；首次=全部、二次=旧摘要+仅新增 ✓(T2 两测试)
- [ ] 占位扫描：每 code step 完整可粘贴、命令带 Expected ✓
- [ ] 类型一致：`prev_summary:str`、`new_msgs=messages[prev:]`、`parts:list[str]`、`convo="\n\n".join(parts)`；`_SESSION_COMPACT_SYSTEM` 仍 module 级 str 常量名不变、被 service.py import 使用 ✓
- [ ] YAGNI：只改输入构造 + prompt 文本；不动触发/水位线/recall/§20/焦点抽取/`[-10:]`/迁移；无新 token 估算 ✓

## Phase Definition of Done

- [ ] 新增 6 测试全绿（characterization 3 不含旧措辞 + 递归首次/二次/回归）
- [ ] 既有 compact/focus/chit-chat/prompt/sse 全套不回归；QA 链路 0 fail
- [ ] import OK
- [ ] 2 feat commit 干净（prompt 重写 / 递归累积）
- [ ] 已交付控制器 Preview MCP 端到端说明（哈密瓜不再丢）
