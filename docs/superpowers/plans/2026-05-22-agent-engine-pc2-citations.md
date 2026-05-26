# 代码解读 Agent 引擎 — Plan C2：引用溯源 cited_entities（后端）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** agent 跑完，把它**实际查过**的 entity_id 收集为 `cited_entities`，随 `done` SSE 事件返回——让前端能展示"本答案基于哪些真实实体"，治幻觉（你 §22 王山河 bug 的同源防护）。

**Architecture:** `ReActSynthesizer` 的 loop 里每个 `ToolCall` 都带 `arguments.entity_id`（agent 主动查的实体）。在 `synthesize` / `synthesize_stream` 跨轮累积这些 entity_id（去重），存进 `SynthesizedAnswer.cited_entities`（新字段），sse_emitter 的 `done` 事件透传。

**Tech Stack:** Python 3.12 / pytest（仓库 venv：`./venv/bin/python -m pytest`）。

**设计来源:** Obsidian `[[代码解读Agent引擎-设计]]` §6（引用溯源）+ §8（done 事件加 cited_entities）。

**前置:** Plan A→C1 已落地（8 工具 + loop + thinking SSE）。`_build_tool_usage_hint` 已含"别瞎编 entity_id"约束（Plan B），本计划聚焦**收集 + 透传**，不再加 prompt 约束（已足够）。

**范围边界:** 仅后端 cited_entities 收集 + done 事件。**不含**：前端引用渲染（Plan C-frontend）、todo（C3）、自由格式+开关（C4）。

**关键现状（已确认）:**
- `SynthesizedAnswer`（`synthesizer.py:78`）dataclass 字段：`sections / token_usage / cost_yuan / raw_output`（无 cited_entities）。
- `done` 事件（`sse_emitter.py:318`）：`{session_id, message_id, total_tokens, cost_yuan, latency_ms}`。
- `ReActSynthesizer.synthesize`（`react_synthesizer.py:73-177`）：loop 里 `response.tool_calls`（list[ToolCall]），返回点 2 处（final 解析 + max_iter 兜底），都是 `return SynthesizedAnswer(sections=..., raw_output=...)`。
- `ReActSynthesizer.synthesize_stream`（`react_synthesizer.py:188-308`）：`round_tool_calls`（list[ToolCall]）+ `for tc in round_tool_calls:` 执行循环，返回点 2 处。
- `ToolCall`（`llm_types.py`）有 `.arguments: dict`，KB 工具入参里 entity_id 字段名统一是 `"entity_id"`。

**仓库 / 分支:** `/Users/java/knowledge-engineering-auth`，分支 `release-0513`。

---

## Task 1: `SynthesizedAnswer.cited_entities` 字段 + 在 synthesize/synthesize_stream 收集

**Files:**
- Modify: `src/service/qa_engine/synthesizer.py`（SynthesizedAnswer 加字段）
- Modify: `src/service/qa_engine/react_synthesizer.py`（两方法收集 + 所有返回点传 cited_entities）
- Test: `tests/test_auth/test_qa_react_synthesizer.py`（追加收集用例）

- [ ] **Step 1: 写失败测试**

在 `tests/test_auth/test_qa_react_synthesizer.py` 末尾追加：

```python
@pytest.mark.asyncio
async def test_synthesize_stream_collects_cited_entities():
    """agent 查过的 entity_id 被收集进 SynthesizedAnswer.cited_entities（去重、跨轮累积）。"""
    from src.service.qa_engine.react_synthesizer import ReActSynthesizer
    from src.service.qa_engine.tools.base import Tool, ToolRegistry
    from src.service.qa_engine.llm_types import StreamTextDelta, ToolCall
    from src.service.qa_engine.retriever import RetrievedContext

    # 注册一个假工具，handler 直接回 echo（不需真后端）
    async def _echo_handler(inp):
        return {"entity_id": inp.get("entity_id"), "ok": True}

    reg = ToolRegistry()
    reg.register(Tool(
        name="ke_callees",
        description="x",
        input_schema={"type": "object", "properties": {"entity_id": {"type": "string"}}, "required": ["entity_id"]},
        handler=_echo_handler,
    ))

    # fake LLM：第 1 轮调 ke_callees(method//A)，第 2 轮再调 ke_callees(method//B)，第 3 轮给 final
    class _FakeLLM:
        def __init__(self):
            self._round = 0

        async def complete_stream_with_tools(self, *, messages, tools):
            self._round += 1
            if self._round == 1:
                yield ToolCall(id="c1", name="ke_callees", arguments={"entity_id": "method//A"})
            elif self._round == 2:
                yield ToolCall(id="c2", name="ke_callees", arguments={"entity_id": "method//B"})
            else:
                yield StreamTextDelta(text="## 概述\n答案")

    synth = ReActSynthesizer(llm_provider=_FakeLLM(), tool_registry=reg, max_iterations=5)
    ctx = RetrievedContext(question="q", project_id="proj-a")
    answer = await synth.synthesize_stream(ctx, history=[])

    assert answer.cited_entities == ["method//A", "method//B"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_react_synthesizer.py::test_synthesize_stream_collects_cited_entities -v`
Expected: FAIL —— `AttributeError: 'SynthesizedAnswer' object has no attribute 'cited_entities'`（或断言失败：默认空）

- [ ] **Step 3a: SynthesizedAnswer 加字段**

`src/service/qa_engine/synthesizer.py` 的 `SynthesizedAnswer` dataclass（line 78-92），在 `raw_output` 之后加：

```python
    raw_output: str = ""
    """原始 LLM 输出（debug/记录用）。"""

    cited_entities: list[str] = field(default_factory=list)
    """agent 实际查过的 entity_id（去重，按首次出现序）；引用溯源用（设计 §6）。"""
```

（确认文件顶部已 `from dataclasses import dataclass, field` —— 已有 field 用法，无需加 import。）

- [ ] **Step 3b: synthesize_stream 收集 + 传字段**

在 `react_synthesizer.py` 的 `synthesize_stream` 方法体开头（`messages = [...]` 之后、`for _iteration ...` 之前，约 line 215-217），声明累积列表：

```python
        # 引用溯源（设计 §6）：累积 agent 查过的 entity_id（去重、按首次出现序）
        cited_entities: list[str] = []
```

在工具执行循环 `for tc in round_tool_calls:`（约 line 279）的循环体开头，加收集：

```python
            for tc in round_tool_calls:
                # 收集本次工具调用的 entity_id（引用溯源）
                _eid = tc.arguments.get("entity_id")
                if isinstance(_eid, str) and _eid and _eid not in cited_entities:
                    cited_entities.append(_eid)
                if on_tool_call is not None:
                    ...（保留原有逻辑不动）
```

（只在循环体最前面加那 3 行收集；其余 on_tool_call / _execute_tool_call / messages.append 全保留。）

把该方法**两个**返回点都加 `cited_entities=cited_entities`：
- final 返回（约 line 258-259）：
  ```python
                sections = QASynthesizer._parse_sections(round_content)
                return SynthesizedAnswer(sections=sections, raw_output=round_content, cited_entities=cited_entities)
  ```
- max_iter 兜底返回（约 line 308）：
  ```python
        return SynthesizedAnswer(sections=sections, raw_output=raw, cited_entities=cited_entities)
  ```

- [ ] **Step 3c: synthesize（非流式）同样收集 + 传字段**

在 `react_synthesizer.py` 的 `synthesize` 方法（约 line 73-177）做对称改动：
- 方法体开头（`messages = [...]` 之后、`for _iteration ...` 之前，约 line 100-105）声明：
  ```python
        # 引用溯源（设计 §6）：累积 agent 查过的 entity_id
        cited_entities: list[str] = []
  ```
- 工具执行循环 `for tc in response.tool_calls:`（约 line 138）循环体最前面加：
  ```python
            for tc in response.tool_calls:
                _eid = tc.arguments.get("entity_id")
                if isinstance(_eid, str) and _eid and _eid not in cited_entities:
                    cited_entities.append(_eid)
                ...（保留原有 on_tool_call / _execute_tool_call / messages.append）
  ```
- 两个返回点（final 约 line 116 + 兜底约 line 177）都加 `cited_entities=cited_entities`：
  ```python
                return SynthesizedAnswer(sections=sections, raw_output=raw, cited_entities=cited_entities)
  ```
  （两处都改。）

> 读文件确认 `synthesize` 里返回点的确切变量名（final 那处是 `raw` 还是别的）；按实际变量传，只新增 `cited_entities=cited_entities` 这个 kwarg，不动 sections/raw_output 既有实参。

- [ ] **Step 4: 跑测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_react_synthesizer.py -v`
Expected: PASS（新收集用例 + 既有全过）

- [ ] **Step 5: commit**

```bash
git add src/service/qa_engine/synthesizer.py src/service/qa_engine/react_synthesizer.py tests/test_auth/test_qa_react_synthesizer.py
git commit -m "$(cat <<'EOF'
feat(qa-engine): 收集 cited_entities（引用溯源，治幻觉）

agent 引擎 Plan C2 Phase 6：SynthesizedAnswer 加 cited_entities 字段；synthesize /
synthesize_stream 跨轮累积 agent 查过的 entity_id（去重、按首次出现序），所有返回点透传。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `done` 事件透传 cited_entities + 回归 + 文档

**Files:**
- Modify: `src/service/qa_engine/sse_emitter.py`（done 事件加 cited_entities）
- Test: `tests/test_auth/test_qa_sse_thinking.py`（追加 done 事件源码不变量；或新建 test_qa_sse_citations.py）

- [ ] **Step 1: 写失败测试**

新建 `tests/test_auth/test_qa_sse_citations.py`：

```python
# tests/test_auth/test_qa_sse_citations.py
"""
sse_emitter done 事件透传 cited_entities（设计 §8）。
重依赖异步生成器，沿用源码不变量手法：done 事件 dict 含 cited_entities: answer.cited_entities。
收集逻辑由 test_qa_react_synthesizer 真单测覆盖。
"""
from pathlib import Path


def test_done_event_includes_cited_entities():
    src = Path("src/service/qa_engine/sse_emitter.py").read_text(encoding="utf-8")
    # done 事件块里透传 answer.cited_entities
    assert "answer.cited_entities" in src
    assert '"cited_entities"' in src
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_sse_citations.py -v`
Expected: FAIL（done 事件还没加 cited_entities）

- [ ] **Step 3: 实现**

`src/service/qa_engine/sse_emitter.py` 的 `done` 事件（约 line 318-324），加一行 `cited_entities`：

```python
    yield format_sse("done", {
        "session_id": session_id,
        "message_id": message_id,
        "total_tokens": answer.token_usage,
        "cost_yuan": answer.cost_yuan,
        "latency_ms": latency_ms,
        "cited_entities": answer.cited_entities,
    })
```

- [ ] **Step 4: 跑测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_sse_citations.py -v`
Expected: PASS

- [ ] **Step 5: 回归 + import 自检**

Run: `./venv/bin/python -m pytest tests/test_auth/ -k "qa or sse or stream or react or think or synthes" -q`
Expected: 全 PASS

Run: `./venv/bin/python -c "from src.service.qa_engine.synthesizer import SynthesizedAnswer; a=SynthesizedAnswer(); print('cited_entities' in a.__dataclass_fields__)"`
Expected: `True`

- [ ] **Step 6: 更新设计 §11 + commit**

`[[代码解读Agent引擎-设计]]` §11 Phase 6 标「🔵 后端 cited_entities 完成（Plan C2，commit refs）；前端引用渲染待 Plan C-frontend」。

```bash
git add src/service/qa_engine/sse_emitter.py tests/test_auth/test_qa_sse_citations.py
git commit -m "$(cat <<'EOF'
feat(qa-engine): done 事件透传 cited_entities（引用溯源）

agent 引擎 Plan C2 Phase 6：SSE done 事件加 cited_entities，前端据此渲染"本答案基于
哪些真实实体"可点击跳转。前端渲染见 Plan C-frontend。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Plan C2 完成定义（验收）

1. ✅ `SynthesizedAnswer.cited_entities` 字段；synthesize / synthesize_stream 跨轮去重收集 agent 查过的 entity_id（真单测覆盖）
2. ✅ 所有 4 个返回点（两方法各 2）都传 cited_entities
3. ✅ `done` SSE 事件透传 cited_entities
4. ✅ qa/sse/stream/react/synthes 测试全过，import OK
5. ✅ 设计 §11 Phase 6 后端标记

## 后续计划（不在本 Plan）
- **Plan C-frontend**：前端渲染 thinking 灰字（C1）+ citations 引用（C2）+ todo checklist（C3）
- **Plan C3**：todo（todo_write 元工具 + todo SSE + 前端）
- **Plan C4**：自由格式输出 + `KE_QA_USE_REACT` 默认开 → agent 正式上线
