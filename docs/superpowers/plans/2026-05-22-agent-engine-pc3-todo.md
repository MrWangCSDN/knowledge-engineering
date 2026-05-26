# 代码解读 Agent 引擎 — Plan C3：todo checklist（后端）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 agent 在多步任务时调 `todo_write` 元工具自报进度，sse_emitter 把这次调用转成专属 `todo` SSE 事件（`{items:[{content,status}]}`）透传前端——补齐后端 SSE 事件三件套（thinking ✅ + citations ✅ + todo 🆕），前端后续一次性渲染。

**Architecture:** `todo_write` 是无后端依赖的纯转发元工具（handler 回显 items）。注册进 default registry 后，agent 主动调用时 `ReActSynthesizer` 经 `on_tool_call` 回调通知 `sse_emitter`；`sse_emitter._on_tool_call` 识别 `call.name == "todo_write"`，在 `starting` 阶段把 `arguments.items` 压成 `("todo", {...})` 事件（复用既有 `pending_tool_events` → `format_sse(ev_type, ev_data)` flush 机制），并跳过对它发普通 `tool_call` 事件。

**Tech Stack:** Python 3.12 / pytest（仓库 venv：`./venv/bin/python -m pytest`）。

**设计来源:** Obsidian `[[代码解读Agent引擎-设计]]` §3.3（todo_write 元工具）+ §8（`todo` SSE 事件 `{items:[{content,status}]}`）+ §11 Phase 5。

**前置:** Plan A→C2 已落地（8 工具 + loop + thinking SSE + citations）。

**范围边界:** 仅后端。**不含**：前端 todo checklist 渲染（Plan C-frontend）、自由格式+`KE_QA_USE_REACT` 默认开（Plan C4）。

---

## ⚠️ 规划期发现的前置 bug（Task 1）

`sse_emitter.stream_qa_answer` 在 streaming 路径里**无条件**把 `memory_block=` 传给 `synthesize_stream`（line ~225 的 `stream_kwargs`），非流式路径也把 `memory_block=` 传给 `synthesize`（line ~266）。但经签名核验：

| 方法 | 接受 memory_block | 接受 **kwargs |
|---|---|---|
| `QASynthesizer.synthesize` / `synthesize_stream` | ✅ | ✗ |
| `ReActSynthesizer.synthesize` / `synthesize_stream` | ✗ | ✗ |

→ 用**真** `ReActSynthesizer` 跑 `stream_qa_answer` 会 `TypeError: synthesize_stream() got an unexpected keyword argument 'memory_block'`，被 sse_emitter 的 `try/except` 吞掉 → 发 `error` 事件。即**真 ReAct 流式模式下 thinking/citations/todo 全部到不了前端**。现有测试全用 `MagicMock(spec=ReActSynthesizer)` + 假 `synthesize_stream(..., memory_block=None, **kwargs)` 吸收掉，把 bug 掩盖了（测试注释明说"防 fake 签名落后于真实 synthesize_stream 再致 TypeError"）。

**Task 1 修这个**：给 `ReActSynthesizer.synthesize`/`synthesize_stream` 加 `memory_block` 参数并注入 system prompt（对齐 `QASynthesizer` 的 `with_memory_block` 用法）。这是 C3（及 C1/C2）端到端可用的前置。

---

## 关键现状（已确认）

- **工具基类** `Tool`（`tools/base.py:28`）：`@dataclass(frozen=True, slots=True)`，字段 `name / description / input_schema / handler`；`handler: ToolHandler = Callable[[dict], Awaitable[dict]]`（async）。`ToolRegistry.register` 重名抛 `ValueError`。
- **工具工厂模式**（如 `ke_impact.py`）：模块级 `_XXX_SCHEMA` dict + `def build_xxx_tool(...) -> Tool`，内部 `async def handler(input)`，末尾 `return Tool(name=..., description=..., input_schema=..., handler=handler)`。
- **registry 装配** `tools/__init__.py:43 build_default_registry(*, graph, business_store, code_store=None, method_interp_store=None)`：无条件注册 6 个核心 ke_* + 条件注册 2 个 store 工具。`__all__` 显式列出公开工厂。
- **sse_emitter 事件桥**（`sse_emitter.py`）：
  - `format_sse(event_type, data)`（line 36）→ `event: <type>\ndata: <compact json>\n\n`。
  - `_on_tool_call(phase, call, result=None)`（line 167-181）：`ReActSynthesizer` 调工具前(`"starting"`)/后(`"complete"`)触发；把 `("tool_call", payload)` 压进 `pending_tool_events` list（line 165）。`starting` 时 `payload["arguments"]=call.arguments`；`complete` 时 `payload["result_preview"]=json.dumps(result)[:600]`。
  - 主循环 flush（line 241-243 + 253-255）：`ev_type, ev_data = pending_tool_events.pop(0); yield format_sse(ev_type, ev_data)` —— **`ev_type` 来自 tuple**，所以压 `("todo", {...})` 就会发 `todo` 事件。
  - `is_react = isinstance(synthesizer, ReActSynthesizer)`（line 184）；流式路径 `stream_kwargs` 里 `is_react` 才加 `on_tool_call`/`on_thinking`（line 227-229）。
- **ReActSynthesizer system prompt 构造**（`react_synthesizer.py` `synthesize` line ~92-97、`synthesize_stream` line ~210-213）：
  ```python
  system_text = SYSTEM_PROMPT
  tool_hint = self._build_tool_usage_hint()
  if tool_hint:
      system_text = f"{SYSTEM_PROMPT}\n\n{tool_hint}"
  ```
  两方法各方法体开头 `from src.service.qa_engine.prompts import SYSTEM_PROMPT, build_user_prompt`。`with_memory_block` 也在 `src.service.qa_engine.prompts`（`QASynthesizer` 已用 `with_memory_block(SYSTEM_PROMPT, memory_block)`；`memory_block=None` 时为 identity）。
- **测试基建**：`test_sse_emitter.py` 有真实驱动 `stream_qa_answer` 的 harness（`_build_mock_retriever()` + 收集 events）；ReAct 流式测试用 `MagicMock(spec=ReActSynthesizer)` + 假 `synthesize_stream`（line 334-349 可直接照搬到 todo 测试）。

**仓库 / 分支:** `/Users/java/knowledge-engineering-auth`，分支 `release-0513`。

---

## Task 1: ReActSynthesizer 接受并注入 memory_block（前置 bug 修复）

**Files:**
- Modify: `src/service/qa_engine/react_synthesizer.py`（`synthesize` + `synthesize_stream` 两方法）
- Test: `tests/test_auth/test_qa_react_synthesizer.py`（追加 2 个用例）

- [ ] **Step 1: 写失败测试**

在 `tests/test_auth/test_qa_react_synthesizer.py` 末尾追加：

```python
@pytest.mark.asyncio
async def test_synthesize_stream_accepts_and_injects_memory_block():
    """ReActSynthesizer.synthesize_stream 接受 memory_block 且注入 system prompt（对齐 QASynthesizer）。
    回归点：sse_emitter 无条件透传 memory_block，真 ReActSynthesizer 此前会 TypeError。"""
    from src.service.qa_engine.react_synthesizer import ReActSynthesizer
    from src.service.qa_engine.tools.base import ToolRegistry
    from src.service.qa_engine.llm_types import StreamTextDelta
    from src.service.qa_engine.retriever import RetrievedContext

    captured: dict = {}

    class _FakeLLM:
        async def complete_stream_with_tools(self, *, messages, tools):
            captured["system"] = messages[0]["content"]
            yield StreamTextDelta(text="## 概述\n答案")

    synth = ReActSynthesizer(llm_provider=_FakeLLM(), tool_registry=ToolRegistry(), max_iterations=3)
    ctx = RetrievedContext(question="q", project_id="p")
    # 关键：带 memory_block 调用——修复前这里直接 TypeError
    answer = await synth.synthesize_stream(ctx, history=[], memory_block="【记忆】用户偏好X")

    assert answer.sections  # 没崩
    assert "【记忆】用户偏好X" in captured["system"]  # 记忆注入了 system prompt


@pytest.mark.asyncio
async def test_synthesize_accepts_and_injects_memory_block():
    """非流式 synthesize 同样接受并注入 memory_block。"""
    from unittest.mock import AsyncMock
    from src.service.qa_engine.react_synthesizer import ReActSynthesizer
    from src.service.qa_engine.tools.base import ToolRegistry
    from src.service.qa_engine.llm_types import LLMToolResponse
    from src.service.qa_engine.retriever import RetrievedContext

    llm = AsyncMock()
    llm.complete_with_tools = AsyncMock(return_value=LLMToolResponse(
        content="```json\n{\"sections\": []}\n```", tool_calls=[],
    ))
    synth = ReActSynthesizer(llm_provider=llm, tool_registry=ToolRegistry(), max_iterations=3)
    ctx = RetrievedContext(question="q", project_id="p")
    await synth.synthesize(ctx, history=[], memory_block="【记忆】偏好X")

    sent = llm.complete_with_tools.call_args.kwargs["messages"][0]["content"]
    assert "【记忆】偏好X" in sent
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_react_synthesizer.py -k "memory_block" -v`
Expected: FAIL —— `TypeError: synthesize_stream() got an unexpected keyword argument 'memory_block'`（synthesize 同理）。

- [ ] **Step 3a: synthesize 加参数 + 注入**

`react_synthesizer.py` 的 `synthesize` 方法签名（约 line 74-79）加 `memory_block` 参数：

```python
    async def synthesize(
        self,
        ctx: RetrievedContext,
        history: list[dict[str, Any]] | None = None,
        on_tool_call: Optional[Callable[..., Awaitable[None]]] = None,
        memory_block: str | None = None,
    ) -> SynthesizedAnswer:
```

方法体开头 import 加 `with_memory_block`，并改 system_text 构造（约 line 87-97）：

```python
        from src.service.qa_engine.prompts import SYSTEM_PROMPT, build_user_prompt, with_memory_block
        from src.service.qa_engine.synthesizer import _ctx_to_dict

        user_prompt = build_user_prompt(ctx.question, _ctx_to_dict(ctx))
        # 记忆注入（对齐 QASynthesizer）：memory_block=None 时 with_memory_block 为 identity
        base_system = with_memory_block(SYSTEM_PROMPT, memory_block)
        system_text = base_system
        tool_hint = self._build_tool_usage_hint()
        if tool_hint:
            system_text = f"{base_system}\n\n{tool_hint}"
```

- [ ] **Step 3b: synthesize_stream 加参数 + 注入**

`synthesize_stream` 方法签名（约 line 189-196）加 `memory_block`（放在 `on_tool_call` 之后）：

```python
    async def synthesize_stream(
        self,
        ctx: RetrievedContext,
        history: list[dict[str, Any]] | None = None,
        on_token: Optional[Callable[[str], Awaitable[None]]] = None,
        on_thinking: Optional[Callable[[str], Awaitable[None]]] = None,
        on_tool_call: Optional[Callable[..., Awaitable[None]]] = None,
        memory_block: str | None = None,
    ) -> SynthesizedAnswer:
```

方法体开头（约 line 206-213）做与 3a 完全相同的 import + system_text 改动：

```python
        from src.service.qa_engine.prompts import SYSTEM_PROMPT, build_user_prompt, with_memory_block
        from src.service.qa_engine.synthesizer import _ctx_to_dict

        user_prompt = build_user_prompt(ctx.question, _ctx_to_dict(ctx))
        base_system = with_memory_block(SYSTEM_PROMPT, memory_block)
        system_text = base_system
        tool_hint = self._build_tool_usage_hint()
        if tool_hint:
            system_text = f"{base_system}\n\n{tool_hint}"
```

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_react_synthesizer.py -v`
Expected: PASS（新 2 用例 + 既有全过；既有用例不传 memory_block，`with_memory_block(SYSTEM_PROMPT, None)` 为 identity，system_text 与改前一致，tool 列举类断言不受影响）。

- [ ] **Step 5: commit**

```bash
git add src/service/qa_engine/react_synthesizer.py tests/test_auth/test_qa_react_synthesizer.py
git commit -m "$(cat <<'EOF'
fix(qa-engine): ReActSynthesizer 接受并注入 memory_block

修复 sse_emitter 无条件透传 memory_block 给真 ReActSynthesizer 时的 TypeError
（此前被全 mock 测试用 **kwargs 吸收掉而掩盖）——真 ReAct 流式模式下
thinking/citations/todo 因此到不了前端。synthesize/synthesize_stream 加
memory_block 参数并按 QASynthesizer 方式 with_memory_block 注入 system prompt。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `todo_write` 元工具 + 注册进 default registry

**Files:**
- Create: `src/service/qa_engine/tools/todo_write.py`
- Modify: `src/service/qa_engine/tools/__init__.py`（import + `__all__` + 无条件注册）
- Test: Create `tests/test_auth/test_qa_tool_todo_write.py`
- Test: Modify `tests/test_auth/test_qa_tools_default_registry.py`（核心工具集合 6→7）

- [ ] **Step 1: 写失败测试（工具单测）**

新建 `tests/test_auth/test_qa_tool_todo_write.py`：

```python
"""todo_write 元工具单测（设计 §3.3）：纯回显 items、无后端依赖。"""
import pytest

from src.service.qa_engine.tools.todo_write import build_todo_write_tool


def test_build_todo_write_tool_metadata():
    """工具名 / schema 基本契约。"""
    tool = build_todo_write_tool()
    assert tool.name == "todo_write"
    assert tool.input_schema["required"] == ["items"]
    assert tool.input_schema["properties"]["items"]["type"] == "array"


@pytest.mark.asyncio
async def test_todo_write_handler_echoes_items():
    """handler 纯回显传入的 items（不查后端）。"""
    tool = build_todo_write_tool()
    items = [
        {"content": "分析订单域入口", "status": "in_progress"},
        {"content": "画调用链", "status": "pending"},
    ]
    out = await tool.handler({"items": items})
    assert out["items"] == items
    assert out["count"] == 2


@pytest.mark.asyncio
async def test_todo_write_handler_missing_items_defaults_empty():
    """items 缺失 / 非 list 时兜底空列表，不抛（§3.4 信号哲学）。"""
    tool = build_todo_write_tool()
    assert (await tool.handler({}))["items"] == []
    assert (await tool.handler({"items": "oops"}))["items"] == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_tool_todo_write.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'src.service.qa_engine.tools.todo_write'`

- [ ] **Step 3: 创建 todo_write.py**

新建 `src/service/qa_engine/tools/todo_write.py`：

```python
"""todo_write 元工具：模型多步任务自追踪 checklist（设计 §3.3）。

与 8 个 ke_* KB 工具不同：无后端依赖、无副作用——handler 纯回显当前 todo 列表，
让 LLM 知道"已记录"。真正的前端展示靠 sse_emitter 识别这次调用 → 发 `todo`
SSE 事件转发（设计 §8）。所以本工具不查 / 不写任何 store。
"""
from __future__ import annotations

from typing import Any

from src.service.qa_engine.tools.base import Tool

# status 合法值（设计 §3.3）
_TODO_STATUS_ENUM = ["pending", "in_progress", "completed"]

_TODO_WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "description": "当前 todo 列表；多步任务时自报进度，简单问题不必调用",
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "任务描述"},
                    "status": {
                        "type": "string",
                        "description": "任务状态",
                        "enum": _TODO_STATUS_ENUM,
                    },
                },
                "required": ["content", "status"],
            },
        },
    },
    "required": ["items"],
}


def build_todo_write_tool() -> Tool:
    """构造 todo_write 元工具（无后端依赖，纯状态转发）。"""

    async def handler(input: dict[str, Any]) -> dict[str, Any]:
        # 纯回显：items 原样返回让 LLM 确认"已记录"。前端展示靠 sse_emitter
        # 把这次调用转成 `todo` 事件（设计 §8）。items 缺失/非 list 兜底空列表（不抛，§3.4）。
        items = input.get("items")
        if not isinstance(items, list):
            items = []
        return {"items": items, "count": len(items)}

    return Tool(
        name="todo_write",
        description=(
            "多步任务自追踪 checklist：把当前待办列表 items（每项 {content, status}，"
            "status ∈ pending/in_progress/completed）记录并展示给用户。"
            "仅在多步复杂任务时调用；简单问题无需调用。"
        ),
        input_schema=_TODO_WRITE_SCHEMA,
        handler=handler,
    )
```

- [ ] **Step 4: 跑工具单测确认通过**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_tool_todo_write.py -v`
Expected: PASS（3 用例）

- [ ] **Step 5: 写失败测试（注册）**

改 `tests/test_auth/test_qa_tools_default_registry.py` 的 `test_default_registry_has_all_six_tools`：把期望集合加上 `"todo_write"`，并改名/改 docstring 反映"6 核心 + todo_write meta"：

```python
def test_default_registry_has_core_tools_plus_todo_write() -> None:
    """build_default_registry 把 6 个核心 ke_* + todo_write 元工具全注册进去。"""
    graph = MagicMock()
    store = MagicMock()
    reg = build_default_registry(graph=graph, business_store=store)

    names = {t.name for t in reg.list_tools()}
    assert names == {
        "ke_search",
        "ke_callees",
        "ke_callers",
        "ke_business_interp",
        "ke_table_access",
        "ke_impact",
        "todo_write",
    }
```

（其余用例不动：`test_default_registry_skips_optional_stores_when_absent` 等用 `in` / `not in` 断言，新增 todo_write 不影响。）

- [ ] **Step 6: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_tools_default_registry.py::test_default_registry_has_core_tools_plus_todo_write -v`
Expected: FAIL —— 集合断言不等（todo_write 还没注册）。

- [ ] **Step 7: 注册进 __init__.py**

`src/service/qa_engine/tools/__init__.py`：
1. import（与其它工厂放一起，约 line 22 之后）：
   ```python
   from src.service.qa_engine.tools.todo_write import build_todo_write_tool
   ```
2. `__all__` 加一行（`build_default_registry` 之前）：
   ```python
       "build_todo_write_tool",
   ```
3. `build_default_registry` 里，`build_ke_impact_tool(graph)` 之后、`if code_store is not None:` 之前，无条件注册：
   ```python
       registry.register(build_ke_impact_tool(graph))
       # meta 工具：todo_write 无后端依赖，始终注册（设计 §3.3）
       registry.register(build_todo_write_tool())
   ```
   同步更新 `build_default_registry` docstring 与文件顶部模块 docstring 里"6 个工具"的措辞（如"6 核心 ke_* + todo_write meta"）。

- [ ] **Step 8: 跑测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_tool_todo_write.py tests/test_auth/test_qa_tools_default_registry.py -v`
Expected: PASS（工具单测 + registry 全过）

- [ ] **Step 9: commit**

```bash
git add src/service/qa_engine/tools/todo_write.py src/service/qa_engine/tools/__init__.py tests/test_auth/test_qa_tool_todo_write.py tests/test_auth/test_qa_tools_default_registry.py
git commit -m "$(cat <<'EOF'
feat(qa-engine): 新增 todo_write 元工具 + 注册（多步任务 checklist）

agent 引擎 Plan C3 Phase 5：todo_write 纯转发元工具（无后端依赖，handler 回显
items），无条件注册进 build_default_registry。前端展示靠 sse_emitter 转 todo 事件（下个 commit）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: sse_emitter 识别 todo_write → 发 `todo` 事件 + 回归 + 文档

**Files:**
- Modify: `src/service/qa_engine/sse_emitter.py`（`_on_tool_call` 内）
- Test: Create `tests/test_auth/test_qa_sse_todo.py`（真集成测试，驱动 stream_qa_answer）

- [ ] **Step 1: 写失败测试（集成）**

新建 `tests/test_auth/test_qa_sse_todo.py`：

```python
"""sse_emitter 把 todo_write 工具调用转成 `todo` SSE 事件（设计 §8）。

真集成测试：用 MagicMock(spec=ReActSynthesizer)（确保 isinstance 命中 → on_tool_call 接线）
+ 假 synthesize_stream（触发一次 todo_write 的 on_tool_call），驱动 stream_qa_answer，
断言输出里出现 `todo` 事件且带 items、且不为 todo_write 额外发普通 tool_call。
"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.service.qa_engine.sse_emitter import stream_qa_answer
from src.service.qa_engine.react_synthesizer import ReActSynthesizer
from src.service.qa_engine.retriever import RetrievedContext
from src.service.qa_engine.synthesizer import SynthesizedAnswer
from src.service.qa_engine.llm_types import ToolCall


def _build_mock_retriever():
    r = MagicMock()
    r.retrieve = AsyncMock(return_value=RetrievedContext(question="q", project_id="p"))
    return r


def _data_of(events: list[str], etype: str) -> list[dict]:
    """从 SSE event 字符串列表里挑出某类型事件的 data dict。"""
    out: list[dict] = []
    for e in events:
        if e.startswith(f"event: {etype}\n"):
            line = next(l for l in e.split("\n") if l.startswith("data: "))
            out.append(json.loads(line[len("data: "):]))
    return out


@pytest.mark.asyncio
async def test_stream_emits_todo_event_on_todo_write_call():
    items = [
        {"content": "分析订单域入口", "status": "in_progress"},
        {"content": "画调用链", "status": "pending"},
    ]

    synth = MagicMock(spec=ReActSynthesizer)

    async def fake_stream(ctx, history=None, on_token=None, on_thinking=None,
                          on_tool_call=None, memory_block=None, **kwargs):
        if on_tool_call:
            call = ToolCall(id="t1", name="todo_write", arguments={"items": items})
            await on_tool_call("starting", call)
            await on_tool_call("complete", call, {"items": items, "count": 2})
        if on_token:
            await on_token("答案")
        return SynthesizedAnswer(
            sections=[{"type": "overview", "title": "x", "content": "y", "references": []}],
            token_usage=1,
        )

    synth.synthesize_stream = AsyncMock(side_effect=fake_stream)

    events: list[str] = []
    async for c in stream_qa_answer(
        question="q", project_id="p", session_id="s",
        retriever=_build_mock_retriever(), synthesizer=synth,
    ):
        events.append(c)

    # 出现且只出现 1 个 todo 事件，带完整 items
    todos = _data_of(events, "todo")
    assert len(todos) == 1
    assert todos[0]["items"] == items

    # todo_write 不应再被当普通 tool_call 事件发
    tool_calls = _data_of(events, "tool_call")
    assert all(tc.get("name") != "todo_write" for tc in tool_calls)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_sse_todo.py -v`
Expected: FAIL —— 还没识别 todo_write，`todos` 为空（且会冒出 name=todo_write 的 tool_call 事件）。

- [ ] **Step 3: 实现 todo_write 识别**

`src/service/qa_engine/sse_emitter.py` 的 `_on_tool_call`（约 line 167-181），在函数体最前面（构造 `payload` 之前）加 todo_write 分支：

```python
    async def _on_tool_call(phase: str, call, result=None):
        """ReActSynthesizer 调工具时触发；把事件压栈，主流程 yield 之前 flush。"""
        # todo_write 元工具（设计 §3.3/§8）：不当普通 tool_call 展示，而是转成专属
        # `todo` 事件（前端渲染 checklist）。只在 starting 阶段发（此时 arguments 已带 items）；
        # complete 的 echo 结果无展示价值，直接跳过。
        if call.name == "todo_write":
            if phase == "starting":
                items = call.arguments.get("items", [])
                pending_tool_events.append(("todo", {"items": items}))
            return
        # 只塞最关键字段：name + arguments / result（截断）
        payload: dict = {"phase": phase, "id": call.id, "name": call.name}
        ...（保留原有 starting/complete 逻辑不动）
```

- [ ] **Step 4: 跑测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_sse_todo.py -v`
Expected: PASS

- [ ] **Step 5: 回归 + import 自检**

Run: `./venv/bin/python -m pytest tests/test_auth/ -k "qa or sse or stream or react or think or synthes or tool or todo or registry" -q`
Expected: 全 PASS

Run: `./venv/bin/python -c "from src.service.qa_engine.tools import build_default_registry, build_todo_write_tool; r=build_default_registry(graph=__import__('unittest.mock',fromlist=['MagicMock']).MagicMock(), business_store=__import__('unittest.mock',fromlist=['MagicMock']).MagicMock()); print('todo_write' in {t.name for t in r.list_tools()})"`
Expected: `True`

- [ ] **Step 6: 更新设计 §11 Phase 5 + commit**

`[[代码解读Agent引擎-设计]]` §11 Phase 5 标「🔵 后端 todo_write + todo 事件完成（Plan C3，commit refs）；前端 checklist 待 Plan C-frontend」（沿用 Phase 4/6 范式）。同时 §3.3 状态栏 `todo_write` 🆕→✅。

```bash
git add src/service/qa_engine/sse_emitter.py tests/test_auth/test_qa_sse_todo.py
git commit -m "$(cat <<'EOF'
feat(qa-engine): sse_emitter 识别 todo_write 发 todo 事件（多步进度）

agent 引擎 Plan C3 Phase 5：_on_tool_call 检测 todo_write 调用，把 arguments.items
转成专属 `todo` SSE 事件（{items:[{content,status}]}）透传前端，跳过对它的普通
tool_call 事件。前端 checklist 渲染见 Plan C-frontend。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Plan C3 完成定义（验收）

1. ✅ `ReActSynthesizer.synthesize`/`synthesize_stream` 接受 `memory_block` 并注入 system prompt（真单测覆盖，回归 sse_emitter 透传 TypeError）
2. ✅ `todo_write` 元工具：纯回显 items、无后端依赖（单测覆盖 echo + 缺省兜底）
3. ✅ `build_default_registry` 无条件注册 todo_write（registry 集合断言 + import 自检）
4. ✅ `sse_emitter._on_tool_call` 识别 todo_write → 发 `todo` 事件（`{items}`）、不发普通 tool_call（真集成测试驱动 stream_qa_answer 覆盖）
5. ✅ qa/sse/stream/react/tool/todo/registry 测试全过
6. ✅ 设计 §11 Phase 5 后端标记 + §3.3 todo_write 状态更新

## 后续计划（不在本 Plan）
- **Plan C-frontend**：前端渲染 thinking 灰字（C1）+ citations 引用（C2）+ todo checklist（C3）
- **Plan C4**：自由格式输出（放开 6-段强制解析）+ `KE_QA_USE_REACT` 默认开 → agent 正式上线
