# 代码解读 Agent 引擎 — Plan C1：thinking SSE 事件（后端）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Plan A/A-cont 已产出的 `StreamThinkingDelta` 思考增量从 agent loop 一路接到 SSE `thinking` 事件——让前端（后续 Plan C-frontend）能拿到思考流灰字渲染。

**Architecture:** `ReActSynthesizer.synthesize_stream` 的真流式循环目前只处理 `StreamTextDelta`（→on_token）和 `ToolCall`，`StreamThinkingDelta` 事件被静默丢弃。本计划：① synthesize_stream 加 `on_thinking` 回调 + 处理 `StreamThinkingDelta`；② sse_emitter 加 `pending_thinking` 缓冲 + `_on_thinking` 回调 + 把它作为 `thinking` SSE 事件 flush（与现有 token/tool_call flush 同模式）。

**Tech Stack:** Python 3.12 / 异步生成器 + SSE / pytest（仓库 venv：`./venv/bin/python -m pytest`）。

**设计来源:** Obsidian `[[代码解读Agent引擎-设计]]` §5（thinking 灰字）+ §8（SSE 协议增量：`thinking` 事件 `{delta}`）。

**前置:** Plan A/A-cont/B/B2 已落地（`StreamThinkingDelta` 类型存在、双 provider 都吐它、loop 就绪、8 工具齐）。

**范围边界:** 仅后端 `thinking` SSE 事件接线。**不含**：前端渲染（Plan C-frontend）、citations（Phase 6）、todo（Phase 5）、自由格式 + 开关上线（Phase 7）。

**仓库 / 分支:** `/Users/java/knowledge-engineering-auth`，分支 `release-0513`。

**关键现状:**
- `ReActSynthesizer.synthesize_stream` 签名（`react_synthesizer.py:188`）：`(self, ctx, history=None, on_token=None, on_tool_call=None)`，真流式循环 `react_synthesizer.py:233-243` 用 `if isinstance(event, StreamTextDelta) ... elif isinstance(event, ToolCall)`，**无 StreamThinkingDelta 分支**。
- `react_synthesizer.py:20` import：`from src.service.qa_engine.llm_types import LLMToolResponse, StreamTextDelta, ToolCall`（缺 StreamThinkingDelta）。
- `sse_emitter.py` 流式主循环（`sse_emitter.py:196-248`）：`pending_tokens` + `pending_tool_events` 两个缓冲，`_on_token` 回调攒批，主循环每 5ms flush 成 `token` / tool_call 事件；`stream_kwargs` 动态构造（is_react 才传 on_tool_call）。

---

## Task 1: `synthesize_stream` 加 `on_thinking` 回调 + 处理 `StreamThinkingDelta`

**Files:**
- Modify: `src/service/qa_engine/react_synthesizer.py`（import + synthesize_stream 签名 + 真流式循环分支）
- Test: `tests/test_auth/test_qa_react_synthesizer.py`（追加一个 on_thinking 用例）

- [ ] **Step 1: 写失败测试**

在 `tests/test_auth/test_qa_react_synthesizer.py` 末尾追加：

```python
@pytest.mark.asyncio
async def test_synthesize_stream_forwards_thinking_to_on_thinking():
    """真流式循环把 StreamThinkingDelta 转发到 on_thinking 回调（StreamTextDelta 仍走 on_token）。"""
    from src.service.qa_engine.react_synthesizer import ReActSynthesizer
    from src.service.qa_engine.tools.base import ToolRegistry
    from src.service.qa_engine.llm_types import StreamTextDelta, StreamThinkingDelta
    from src.service.qa_engine.retriever import RetrievedContext

    # fake LLM：有 complete_stream_with_tools（走真流路径），先吐思考再吐答案、无 tool_calls（即 final）
    class _FakeStreamLLM:
        async def complete_stream_with_tools(self, *, messages, tools):
            yield StreamThinkingDelta(text="先看调用方")
            yield StreamTextDelta(text="## 概述\n答案正文")

    synth = ReActSynthesizer(
        llm_provider=_FakeStreamLLM(),
        tool_registry=ToolRegistry(),
        max_iterations=3,
    )

    thinking_chunks: list[str] = []
    token_chunks: list[str] = []

    async def _on_thinking(t): thinking_chunks.append(t)
    async def _on_token(t): token_chunks.append(t)

    # RetrievedContext 是 dataclass：question + project_id 必填，其余字段有默认值
    ctx = RetrievedContext(question="VetController 调了谁？", project_id="proj-a")
    await synth.synthesize_stream(
        ctx, history=[], on_token=_on_token, on_thinking=_on_thinking,
    )

    assert "".join(thinking_chunks) == "先看调用方"
    assert "".join(token_chunks) == "## 概述\n答案正文"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_react_synthesizer.py::test_synthesize_stream_forwards_thinking_to_on_thinking -v`
Expected: FAIL —— `synthesize_stream() got an unexpected keyword argument 'on_thinking'`

- [ ] **Step 3: 实现**

3a. `src/service/qa_engine/react_synthesizer.py` 第 20 行 import 加 `StreamThinkingDelta`：

```python
from src.service.qa_engine.llm_types import LLMToolResponse, StreamTextDelta, StreamThinkingDelta, ToolCall
```

3b. `synthesize_stream` 签名（约 line 188-194）加 `on_thinking` 参数（放在 on_token 之后、on_tool_call 之前）：

```python
    async def synthesize_stream(
        self,
        ctx: RetrievedContext,
        history: list[dict[str, Any]] | None = None,
        on_token: Optional[Callable[[str], Awaitable[None]]] = None,
        on_thinking: Optional[Callable[[str], Awaitable[None]]] = None,
        on_tool_call: Optional[Callable[..., Awaitable[None]]] = None,
    ) -> SynthesizedAnswer:
```

3c. 真流式循环（约 line 233-243）：在 `if isinstance(event, StreamTextDelta): ...` 块之后、`elif isinstance(event, ToolCall):` 之前，插入 StreamThinkingDelta 分支。即把：

```python
                    if isinstance(event, StreamTextDelta):
                        round_text_buf.append(event.text)
                        # 立刻推给 on_token（首 token 即可见的关键）
                        if on_token is not None:
                            try:
                                await on_token(event.text)
                            except Exception:
                                pass
                    elif isinstance(event, ToolCall):
                        round_tool_calls.append(event)
```

改为（在两个分支之间插入 thinking 分支）：

```python
                    if isinstance(event, StreamTextDelta):
                        round_text_buf.append(event.text)
                        # 立刻推给 on_token（首 token 即可见的关键）
                        if on_token is not None:
                            try:
                                await on_token(event.text)
                            except Exception:
                                pass
                    elif isinstance(event, StreamThinkingDelta):
                        # 思考增量 → on_thinking（设计 §5 灰字）；不进 round_text_buf（不污染答案）
                        if on_thinking is not None:
                            try:
                                await on_thinking(event.text)
                            except Exception:
                                pass
                    elif isinstance(event, ToolCall):
                        round_tool_calls.append(event)
```

（注意原 `except Exception:` 块内可能有具体语句，按文件实际内容保留；这里只新增 thinking elif 分支，不动 StreamTextDelta / ToolCall 既有逻辑。）

- [ ] **Step 4: 跑测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_react_synthesizer.py -v`
Expected: PASS（新 on_thinking 用例 + 既有全过）

- [ ] **Step 5: commit**

```bash
git add src/service/qa_engine/react_synthesizer.py tests/test_auth/test_qa_react_synthesizer.py
git commit -m "$(cat <<'EOF'
feat(qa-engine): synthesize_stream 加 on_thinking 回调转发 StreamThinkingDelta

agent 引擎 Plan C1 Phase 4：真流式循环新增 StreamThinkingDelta 分支 → on_thinking 回调，
思考增量不进答案 buffer（不污染正文）。为 sse_emitter 的 thinking SSE 事件铺路。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: sse_emitter 发 `thinking` SSE 事件

**Files:**
- Modify: `src/service/qa_engine/sse_emitter.py`（pending_thinking 缓冲 + _on_thinking + stream_kwargs + flush）
- Test: `tests/test_auth/test_qa_sse_thinking.py`（新建，源码不变量 + 契约校验）

**测试策略说明:** sse_emitter 的 `stream_qa_answer` 是个重度依赖 synthesizer/ctx 的异步生成器，整体跑过脆（沿用本仓 chat.test.ts / sse 既有手法）。这里用**源码不变量**断言接线正确：on_thinking 被构造、传入 stream_kwargs、pending_thinking 作为 `thinking` 事件 flush。Task 1 已对 synthesize_stream 的 on_thinking 行为做了真单测，两者合起来覆盖链路。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_auth/test_qa_sse_thinking.py
"""
sse_emitter 的 thinking SSE 事件接线（设计 §8）。
stream_qa_answer 是重依赖异步生成器，沿用本仓源码不变量手法校验装配契约：
  - 构造了 _on_thinking 回调
  - is_react 时把 on_thinking 传进 stream_kwargs
  - pending_thinking 作为 'thinking' 事件 flush
synthesize_stream 的 on_thinking 真行为由 test_qa_react_synthesizer 覆盖。
"""
from pathlib import Path


def _src() -> str:
    return Path("src/service/qa_engine/sse_emitter.py").read_text(encoding="utf-8")


def test_sse_emitter_defines_on_thinking_callback():
    src = _src()
    assert "pending_thinking" in src
    assert "async def _on_thinking" in src


def test_sse_emitter_passes_on_thinking_to_stream_kwargs():
    src = _src()
    # is_react 分支把 on_thinking 接进 stream_kwargs
    assert 'stream_kwargs["on_thinking"]' in src


def test_sse_emitter_flushes_thinking_event():
    src = _src()
    # pending_thinking 作为 'thinking' 事件 yield（format_sse("thinking", ...)）
    assert 'format_sse("thinking"' in src
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_sse_thinking.py -v`
Expected: FAIL（3 个断言都因 sse_emitter 还没接线而失败）

- [ ] **Step 3: 实现**

在 `src/service/qa_engine/sse_emitter.py`：

3a. 在 `pending_tokens: list[str] = []`（约 line 197）附近，加 thinking 缓冲：

```python
    pending_tokens: list[str] = []
    pending_thinking: list[str] = []
    token_batcher = TokenBatcher(min_chars=1, max_ms=10)
```

3b. 在 `_on_token` 定义（约 line 200-204）之后，加 `_on_thinking` 回调（思考不攒批，直接入栈——频率低、要尽快可见）：

```python
    async def _on_thinking(delta: str) -> None:
        """LLM 思考增量直接入栈，主循环 flush 成 SSE thinking 事件（设计 §5 灰字）。"""
        if delta:
            pending_thinking.append(delta)
```

3c. 在 `stream_kwargs` 构造（约 line 216-222），is_react 分支里把 on_thinking 接进去（与 on_tool_call 同处）：

```python
            stream_kwargs: dict[str, Any] = {
                "history": history,
                "on_token": _on_token,
                "memory_block": memory_block,
            }
            if is_react:
                stream_kwargs["on_tool_call"] = _on_tool_call
                stream_kwargs["on_thinking"] = _on_thinking
```

3d. 在主 flush 循环里（约 line 228-244），凡 flush pending_tool_events / pending_tokens 的地方，**前面**加一段 flush pending_thinking（思考应在该轮 token 前出）。即 while 循环内 + task 完成后两处，各加：

```python
                while pending_thinking:
                    delta = pending_thinking.pop(0)
                    yield format_sse("thinking", {"delta": delta})
```

具体：把 `while not task.done():` 循环体改为（thinking flush 放最前）：

```python
            while not task.done():
                # flush 思考增量（设计 §5 灰字；在 token 前出）
                while pending_thinking:
                    delta = pending_thinking.pop(0)
                    yield format_sse("thinking", {"delta": delta})
                # flush pending tool_call events（ReAct 模式才有）
                while pending_tool_events:
                    ev_type, ev_data = pending_tool_events.pop(0)
                    yield format_sse(ev_type, ev_data)
                # flush 当前 pending tokens
                while pending_tokens:
                    delta = pending_tokens.pop(0)
                    yield format_sse("token", {"delta": delta})
                await asyncio.sleep(0.005)
```

并在 task 完成后的收尾 flush 段（`while pending_tool_events: ... while pending_tokens: ...` 那一段）前面也加一次 thinking flush：

```python
            # task 完成后 buffer 里可能还有最后几个事件
            while pending_thinking:
                delta = pending_thinking.pop(0)
                yield format_sse("thinking", {"delta": delta})
            while pending_tool_events:
                ev_type, ev_data = pending_tool_events.pop(0)
                yield format_sse(ev_type, ev_data)
            while pending_tokens:
                delta = pending_tokens.pop(0)
                yield format_sse("token", {"delta": delta})
```

- [ ] **Step 4: 跑测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_sse_thinking.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: commit**

```bash
git add src/service/qa_engine/sse_emitter.py tests/test_auth/test_qa_sse_thinking.py
git commit -m "$(cat <<'EOF'
feat(qa-engine): sse_emitter 发 thinking SSE 事件（设计 §5/§8）

agent 引擎 Plan C1 Phase 4：pending_thinking 缓冲 + _on_thinking 回调，is_react 时
接进 stream_kwargs，主循环把思考增量作为 'thinking' 事件 flush（在 token 前出）。
前端灰字渲染见后续 Plan C-frontend。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: 回归 + 设计文档进度更新

- [ ] **Step 1: 跑 qa/sse/stream/react/thinking 测试全集**

Run: `./venv/bin/python -m pytest tests/test_auth/ -k "qa or sse or stream or react or think or dashscope or minimax" -q`
Expected: 全 PASS

- [ ] **Step 2: import 自检**

Run: `./venv/bin/python -c "import src.service.qa_engine.sse_emitter; from src.service.qa_engine.react_synthesizer import ReActSynthesizer; print('OK')"`
Expected: `OK`

- [ ] **Step 3: 更新 Obsidian 设计文档 §11**

把 `[[代码解读Agent引擎-设计]]` §11 Phase 4 标「🔵 后端 thinking SSE 完成（Plan C1，commit refs）；前端灰字渲染待 Plan C-frontend」。

---

## Plan C1 完成定义（验收）

1. ✅ `synthesize_stream` 把 `StreamThinkingDelta` 转发到 `on_thinking`（真单测覆盖），不污染答案 buffer
2. ✅ sse_emitter 构造 `_on_thinking`、is_react 传入 stream_kwargs、把思考作为 `thinking` 事件 flush（在 token 前）
3. ✅ qa/sse/stream/react 测试全过，import OK
4. ✅ 设计 §11 Phase 4 后端部分标记完成

## 后续计划（不在本 Plan）

- **Plan C-frontend**：前端 `chat.ts` SSE parser 加 `thinking` case + `ThinkingBlock` 灰字可折叠组件
- **Plan C2**：Phase 6 citations（cited_entities 收集 + done 事件 + 前端引用）
- **Plan C3**：Phase 5 todo（todo_write 元工具 + todo SSE 事件 + 前端 checklist）
- **Plan C4**：Phase 7 自由格式输出 + `KE_QA_USE_REACT` 默认开（agent 正式上线）
