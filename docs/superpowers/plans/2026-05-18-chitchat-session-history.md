# chit-chat 会话级多轮（修丢历史缺陷）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 修掉 chit-chat 合成路径完全丢弃对话历史的缺陷——让 chit-chat 像 KG 路径一样带最近若干轮原文（已由 P2② 按 token 预算裁好），实现真正的 session 级多轮闲聊。

**Architecture:** 方案 A（DRY，对齐 Claude Code「近轮 verbatim ⊕ 旧轮 summary」）：`prompts.py` 抽共享 `_format_history` + 新 `build_chitchat_user_prompt`；`synthesizer.py` 两个 chit-chat 方法接 `history` 并改用新 prompt 构造；`synthesize`/`synthesize_stream` 的 chit-chat 分发补传 `history`。纯参数透传 + 字符串拼接，无新 IO。KG 路径逻辑/§18 压缩/router/前端**全不动**；`history=None` 默认 → chit-chat 无历史时与改前逐字节一致。

**Tech Stack:** Python / pytest（沿用 `tests/test_auth` 既有 capturing-fake 风格）

**Spec:** `/Users/java/obsidian/01 Engineering/knowledge-engineering/记忆系统-设计.md` §20（方案 A 定稿）+ §4.3/§7/§18

**Repo:** `/Users/java/knowledge-engineering-auth`，分支 `release-0513`（沿用，无 worktree）。逐任务提交（已授权）。

---

## 现状基线（已核对真实代码 2026-05-18）

`src/service/qa_engine/prompts.py`（`build_user_prompt_with_history` 当前完整体，行 ~244）：
```python
def build_user_prompt_with_history(
    question: str,
    context: dict[str, Any],
    history: list[dict] | None = None,
) -> str:
    """把历史轮直接拼到 question 前面（不在此压缩）。

    P2②（[[记忆系统-设计]] §18）起：router 进流前已按模型窗口 token 预算裁过
    body.history（更早轮由 system 记忆块 working_summary+focus 顶替），传入此处
    的已是裁好的最近若干轮。此处的 history[-10:] 仅作冗余兜底硬上限，正常不会触发。
    """
    if not history:
        return build_user_prompt(question, context)

    base = build_user_prompt(question, context)
    history_text = "\n".join(
        f"[{m.get('role', '?')}] {m.get('content', '')[:200]}" for m in history[-10:]
    )
    return f"【对话历史】\n{history_text}\n\n{base}"
```
synthesizer.py imports（行 ~16-22）：`from src.service.qa_engine.prompts import (SYSTEM_PROMPT, _CHIT_CHAT_SYSTEM, build_user_prompt, build_user_prompt_with_history, with_memory_block)`。

`_synthesize_chit_chat`（行 77-96，当前）：
```python
    async def _synthesize_chit_chat(
        self, ctx: RetrievedContext, *, memory_block: str | None = None
    ) -> SynthesizedAnswer:
        """v1.2 chit-chat 闲聊路径：用专属 prompt 调 LLM，返回单段 chit-chat section。
        设计：[[chit-chat-闲聊路径-设计]] §4.3, §4.4；记忆注入见 [[记忆系统-设计]] §7。"""
        reply = await self.llm.complete(
            system=with_memory_block(_CHIT_CHAT_SYSTEM, memory_block),
            user=ctx.question,
        )
        return SynthesizedAnswer(
            sections=[{"type": "chit-chat", "title": "", "content": reply, "references": []}],
            token_usage=len(reply.split()),
            cost_yuan=0.0,
            raw_output=reply,
        )
```
`_synthesize_chit_chat_stream`（行 98-126，当前）：
```python
    async def _synthesize_chit_chat_stream(
        self,
        ctx: RetrievedContext,
        on_token: Optional[Callable[[str], Awaitable[None]]] = None,
        *,
        memory_block: str | None = None,
    ) -> SynthesizedAnswer:
        """v1.2 chit-chat 流式版：边收 LLM token 边调 on_token。
        设计：[[chit-chat-闲聊路径-设计]] §4.3, §4.6；记忆注入见 [[记忆系统-设计]] §7。"""
        parts: list[str] = []
        async for tok in self.llm.complete_stream(
            system=with_memory_block(_CHIT_CHAT_SYSTEM, memory_block),
            user=ctx.question,
        ):
            parts.append(tok)
            if on_token is not None:
                await on_token(tok)
        reply = "".join(parts)
        return SynthesizedAnswer(
            sections=[{"type": "chit-chat", "title": "", "content": reply, "references": []}],
            token_usage=len(reply.split()),
            ...
        )
```
Dispatch（当前）：`synthesize` 行 143-144 `if ctx.skill_id == "chit-chat": return await self._synthesize_chit_chat(ctx, memory_block=memory_block)`；`synthesize_stream` 行 207-209 `if ctx.skill_id == "chit-chat": return await self._synthesize_chit_chat_stream(ctx, on_token=on_token, memory_block=memory_block)`。两函数签名本就已收 `history`（KG 分支用 `build_user_prompt_with_history(ctx.question, ctx_dict, history=history)`）。

`tests/test_auth/test_memory_service.py` 已有 `_CapturingLLM`（`async def complete(*, system, user, **kw)` 记 `last_system`）、`_CapturingStreamLLM`（含 `complete` + `complete_stream` async-gen）、`_ctx(skill_id="architecture")`→`RetrievedContext(question="下单流程怎么走", project_id="test-project", entry_candidates=[], callees_by_entry={}, callers_by_entry={}, table_access_by_entry={}, skill_id=skill_id)`、`QASynthesizer`，`pytest.mark.asyncio`。`tests/test_auth/test_qa_prompts.py` 测 prompts 纯函数。

---

## File Structure

| 文件 | 改动 |
|---|---|
| `src/service/qa_engine/prompts.py` | 抽 `_format_history(history)->str`（防御版，搬现有格式化逻辑）；`build_user_prompt_with_history` 改为调它（KG 输出逐字节不变）；新增 `build_chitchat_user_prompt(question, history=None)` |
| `src/service/qa_engine/synthesizer.py` | import 加 `build_chitchat_user_prompt`；两 chit-chat 方法签名加 `history=None` 且 `user=` 改用新 prompt；`synthesize`/`synthesize_stream` chit-chat 分发补传 `history=history` |
| `tests/test_auth/test_qa_prompts.py` | 追加 `_format_history` + `build_chitchat_user_prompt` + `build_user_prompt_with_history` 逐字节回归 测试 |
| `tests/test_auth/test_memory_service.py` | 追加 chit-chat 同步/流式带 history + 向后兼容 + 分发透传 测试（复用既有 capturing fake 风格） |

---

## Task 1: prompts.py —— 抽 `_format_history` + `build_chitchat_user_prompt`

**Files:** Modify `src/service/qa_engine/prompts.py`; Test `tests/test_auth/test_qa_prompts.py`

- [ ] **Step 1: 失败测试** —— 追加到 `tests/test_auth/test_qa_prompts.py` 末尾：

```python
# ───────── 会话级多轮：_format_history / build_chitchat_user_prompt（spec §20）─────────
from src.service.qa_engine.prompts import (
    _format_history,
    build_chitchat_user_prompt,
    build_user_prompt_with_history,
    build_user_prompt,
)


def test_format_history_basic_and_truncation():
    h = [{"role": "user", "content": "x" * 250}, {"role": "assistant", "content": "好的"}]
    out = _format_history(h)
    assert out == f"[user] {'x' * 200}\n[assistant] 好的"   # 每条截 200


def test_format_history_keeps_last_10():
    h = [{"role": "user", "content": f"m{i}"} for i in range(15)]
    out = _format_history(h)
    assert out.count("\n") == 9                 # 只保留最近 10 条 → 9 个换行
    assert "[user] m5" in out and "[user] m14" in out
    assert "[user] m4" not in out


def test_format_history_defensive():
    assert _format_history(None) == ""
    assert _format_history([]) == ""
    assert _format_history("notlist") == ""
    # 非 dict 项跳过，不抛
    assert _format_history([{"role": "user", "content": "ok"}, 123, None]) == "[user] ok"
    # 缺字段用默认
    assert _format_history([{}]) == "[?] "


def test_build_chitchat_user_prompt_with_history():
    h = [{"role": "user", "content": "我喜欢吃西瓜"}, {"role": "assistant", "content": "西瓜解暑"}]
    out = build_chitchat_user_prompt("我喜欢什么水果", h)
    assert out == "【对话历史】\n[user] 我喜欢吃西瓜\n[assistant] 西瓜解暑\n\n我喜欢什么水果"


def test_build_chitchat_user_prompt_no_history_is_bare_question():
    assert build_chitchat_user_prompt("你好", None) == "你好"
    assert build_chitchat_user_prompt("你好", []) == "你好"


def test_build_user_prompt_with_history_byte_identical_after_refactor():
    """KG 路径回归守护：抽取 _format_history 后，build_user_prompt_with_history
    对正常 dict 历史的输出必须与既有格式逐字节一致。"""
    ctx = {"entry_candidates": [], "skill_id": "architecture"}
    h = [{"role": "user", "content": "Q1"}, {"role": "assistant", "content": "A1"}]
    base = build_user_prompt("问题", ctx)
    expected = f"【对话历史】\n[user] Q1\n[assistant] A1\n\n{base}"
    assert build_user_prompt_with_history("问题", ctx, history=h) == expected
    # 无历史 → 退回 base（不变）
    assert build_user_prompt_with_history("问题", ctx, history=None) == base
```

- [ ] **Step 2: 跑，确认失败** —— `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_qa_prompts.py -q`
  Expected: FAIL —— `ImportError: cannot import name '_format_history'`

- [ ] **Step 3: 实现** —— 在 `src/service/qa_engine/prompts.py`，于 `build_user_prompt_with_history` 定义**之前**插入：

```python
def _format_history(history: list[dict] | None) -> str:
    """把最近 ≤10 轮历史格式化为多行 `[role] content(≤200字)`。

    KG 与 chit-chat 共用单一来源（DRY）。防御：history 非 list/None/空 → ""；
    非 dict 项跳过（正常全 dict 时输出与既有逐字节一致）。
    """
    if not isinstance(history, list) or not history:
        return ""
    lines: list[str] = []
    for m in history[-10:]:
        if not isinstance(m, dict):
            continue
        lines.append(f"[{m.get('role', '?')}] {m.get('content', '')[:200]}")
    return "\n".join(lines)


def build_chitchat_user_prompt(
    question: str, history: list[dict] | None = None
) -> str:
    """chit-chat 专属 user prompt：带最近历史（无 KG 6 段脚手架）。

    history 空/None → 仅 question（保持 chit-chat 无历史时旧行为，逐字节一致）。
    设计：[[记忆系统-设计]] §20。
    """
    h = _format_history(history)
    if not h:
        return question
    return f"【对话历史】\n{h}\n\n{question}"
```

然后把 `build_user_prompt_with_history` 的函数体中：
```python
    base = build_user_prompt(question, context)
    history_text = "\n".join(
        f"[{m.get('role', '?')}] {m.get('content', '')[:200]}" for m in history[-10:]
    )
    return f"【对话历史】\n{history_text}\n\n{base}"
```
替换为：
```python
    base = build_user_prompt(question, context)
    return f"【对话历史】\n{_format_history(history)}\n\n{base}"
```
（`if not history: return build_user_prompt(question, context)` 早返回守卫保持不变；正常 dict 历史下 `_format_history` 输出 == 原 join，KG 逐字节不变。）

- [ ] **Step 4: 跑，确认通过** —— `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_qa_prompts.py -q`
  Expected: 全 passed（6 新 + 既有 prompts 测试不回归——逐字节回归测试守住 KG 输出不变）

- [ ] **Step 5: Commit**
```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/qa_engine/prompts.py tests/test_auth/test_qa_prompts.py
git commit -m "$(cat <<'EOF'
feat(qa): 抽共享 _format_history + build_chitchat_user_prompt（DRY，KG 逐字节不变，TDD）

为修 chit-chat 丢历史做准备：把 build_user_prompt_with_history 的历史格式化抽成
共享 _format_history（加 None/非dict 防御），新增 chit-chat 专属带历史 prompt。
设计 [[记忆系统-设计]] §20。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: synthesizer.py —— chit-chat 两方法接 history + 分发透传

**Files:** Modify `src/service/qa_engine/synthesizer.py`; Test `tests/test_auth/test_memory_service.py`

- [ ] **Step 1: 失败测试** —— 追加到 `tests/test_auth/test_memory_service.py` 末尾（复用文件内已有的 `QASynthesizer`、`_ctx`、`pytest`；新建只读 user 的 capturing fake）：

```python
# ───────── chit-chat 会话级多轮：history 接入（spec §20）─────────

class _CapUserLLM:
    """记录最后一次 complete/complete_stream 的 user 入参。"""
    def __init__(self):
        self.last_user = None

    async def complete(self, *, system, user, **kw):
        self.last_user = user
        return "ok"

    async def complete_stream(self, *, system, user, **kw):
        self.last_user = user
        yield "ok"


_HIST = [
    {"role": "user", "content": "我喜欢吃西瓜"},
    {"role": "assistant", "content": "西瓜解暑"},
]


@pytest.mark.asyncio
async def test_chitchat_sync_includes_history():
    llm = _CapUserLLM()
    syn = QASynthesizer(llm)
    await syn.synthesize(_ctx(skill_id="chit-chat"), history=_HIST)
    assert "【对话历史】" in llm.last_user
    assert "我喜欢吃西瓜" in llm.last_user
    assert llm.last_user.endswith("下单流程怎么走")   # _ctx 的 question 在末尾


@pytest.mark.asyncio
async def test_chitchat_stream_includes_history():
    llm = _CapUserLLM()
    syn = QASynthesizer(llm)
    await syn.synthesize_stream(_ctx(skill_id="chit-chat"), history=_HIST)
    assert "【对话历史】" in llm.last_user and "我喜欢吃西瓜" in llm.last_user


@pytest.mark.asyncio
async def test_chitchat_no_history_is_bare_question_backward_compat():
    llm = _CapUserLLM()
    syn = QASynthesizer(llm)
    await syn.synthesize(_ctx(skill_id="chit-chat"))            # 不传 history
    assert llm.last_user == "下单流程怎么走"                     # 与改前逐字节一致
    llm2 = _CapUserLLM()
    await QASynthesizer(llm2).synthesize_stream(_ctx(skill_id="chit-chat"))
    assert llm2.last_user == "下单流程怎么走"
```

- [ ] **Step 2: 跑，确认失败** —— `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_service.py -q`
  Expected: FAIL —— `test_chitchat_sync_includes_history` 断言失败（`【对话历史】` 不在 user，因 chit-chat 仍发裸 `ctx.question`）

- [ ] **Step 3: 实现** —— `src/service/qa_engine/synthesizer.py`：

(a) import：现有 `from src.service.qa_engine.prompts import (SYSTEM_PROMPT, _CHIT_CHAT_SYSTEM, build_user_prompt, build_user_prompt_with_history, with_memory_block)` → 末尾加 `, build_chitchat_user_prompt`。

(b) `_synthesize_chit_chat` 签名与 user：
```python
    async def _synthesize_chit_chat(
        self, ctx: RetrievedContext, *, memory_block: str | None = None,
        history: list[dict] | None = None,
    ) -> SynthesizedAnswer:
```
其内 `reply = await self.llm.complete(system=with_memory_block(_CHIT_CHAT_SYSTEM, memory_block), user=ctx.question,)` 的 `user=ctx.question,` 改为 `user=build_chitchat_user_prompt(ctx.question, history),`。docstring 末追加：`§20：带最近历史原文（旧轮由 memory_block 的 working_summary 覆盖）。`

(c) `_synthesize_chit_chat_stream` 签名与 user：
```python
    async def _synthesize_chit_chat_stream(
        self,
        ctx: RetrievedContext,
        on_token: Optional[Callable[[str], Awaitable[None]]] = None,
        *,
        memory_block: str | None = None,
        history: list[dict] | None = None,
    ) -> SynthesizedAnswer:
```
其内 `async for tok in self.llm.complete_stream(system=with_memory_block(_CHIT_CHAT_SYSTEM, memory_block), user=ctx.question,):` 的 `user=ctx.question,` 改为 `user=build_chitchat_user_prompt(ctx.question, history),`。docstring 同样追加 §20 一句。

(d) `synthesize` 的 chit-chat 分发（行 ~143-144）：
```python
        if ctx.skill_id == "chit-chat":
            return await self._synthesize_chit_chat(ctx, memory_block=memory_block)
```
→
```python
        if ctx.skill_id == "chit-chat":
            return await self._synthesize_chit_chat(
                ctx, memory_block=memory_block, history=history
            )
```

(e) `synthesize_stream` 的 chit-chat 分发（行 ~207-209）：
```python
        if ctx.skill_id == "chit-chat":
            return await self._synthesize_chit_chat_stream(
                ctx, on_token=on_token, memory_block=memory_block
            )
```
→
```python
        if ctx.skill_id == "chit-chat":
            return await self._synthesize_chit_chat_stream(
                ctx, on_token=on_token, memory_block=memory_block, history=history
            )
```
（`synthesize`/`synthesize_stream` 函数签名本就有 `history` 参数；KG 分支 `build_user_prompt_with_history(ctx.question, ctx_dict, history=history)` 不动。其余一律不动。）

- [ ] **Step 4: 跑，确认通过** —— `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_service.py -q`
  Expected: 全 passed（3 新 + 既有 memory/synthesizer 注入测试不回归——`history=None` 默认 + 仅 chit-chat user 变化）

- [ ] **Step 5: 回归** —— `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_qa_synthesizer.py tests/test_auth/test_qa_router_chitchat.py tests/test_auth/test_sse_emitter.py -q`
  Expected: 全 passed（chit-chat 既有行为：无 history 时 user 仍是裸 question；KG 路径未触碰）。若基线绿→红即真回归须查修。

- [ ] **Step 6: Commit**
```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/qa_engine/synthesizer.py tests/test_auth/test_memory_service.py
git commit -m "$(cat <<'EOF'
fix(qa): chit-chat 接入对话历史——修每轮无状态缺陷（会话级多轮，TDD）

_synthesize_chit_chat[_stream] 加 history 参数、user 改用 build_chitchat_user_prompt；
synthesize/synthesize_stream chit-chat 分发补传 history（本就已收）。history=None
默认 → 无历史时逐字节一致向后兼容。对齐 Claude Code 近轮 verbatim。设计 §20。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: 回归验证（无新文件/commit）

- [ ] **Step 1: prompts + synthesizer + memory 全套** —— `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_qa_prompts.py tests/test_auth/test_memory_service.py tests/test_auth/test_qa_synthesizer.py tests/test_auth/test_qa_router_chitchat.py tests/test_auth/test_sse_emitter.py -q`
  Expected: 全 passed（新增 9 + 既有不回归；尤其 `test_build_user_prompt_with_history_byte_identical_after_refactor` 守住 KG 逐字节不变）

- [ ] **Step 2: import 自检** —— `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -c "from src.service.qa_engine.prompts import _format_history, build_chitchat_user_prompt; import src.service.qa_engine.synthesizer; print('import OK')"`
  Expected: `import OK`

- [ ] **Step 3: QA 链路广回归** —— `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/ -q -k "qa or sse or memory or prompt or chitchat or context_budget" -p no:warnings`
  Expected: 0 fail（基线本会话 326/0 + 本增量新增 9 → 335/0）

> 端到端（控制器用 Preview MCP 跑，不在 subagent 范围）：驱动跑着的产品，新对话「我喜欢吃西瓜」→「我喜欢什么水果」→ chit-chat 应能据上一轮答出「西瓜」；截图留证。

---

## Self-Review（实施者过一遍）

- [ ] spec §20 逐条：方案 A 抽 `_format_history` + `build_chitchat_user_prompt` ✓(T1)；KG `build_user_prompt_with_history` 逐字节不变（回归测试守护）✓(T1)；两 chit-chat 方法接 history、user 改用新 prompt ✓(T2)；分发补传 history ✓(T2)；history=None 向后兼容逐字节一致 ✓(T1+T2 测试)；KG/§18/router/前端不动 ✓
- [ ] 占位扫描：每 code step 完整可粘贴、命令带 Expected ✓
- [ ] 类型一致：`_format_history(history)->str`、`build_chitchat_user_prompt(question, history=None)->str`、两 chit-chat 方法 `history: list[dict]|None=None`、分发 `history=history` 跨任务一致 ✓
- [ ] YAGNI：只 DRY 抽取 + chit-chat 接历史；无新 token 估算、不改 §18、不重构两路径、不动前端 ✓

## Phase Definition of Done

- [ ] 新增 9 测试全绿；prompts/synthesizer/memory/chitchat/sse 全套不回归
- [ ] `test_build_user_prompt_with_history_byte_identical_after_refactor` 通过（KG 输出零变化）
- [ ] QA 链路 335/0（不回归本会话 326/0 基线）
- [ ] import OK
- [ ] 2 feat commit 干净（prompts 抽取 / synthesizer 接线）
- [ ] 已交付控制器 Preview MCP 端到端验证说明（吃西瓜→什么水果）
