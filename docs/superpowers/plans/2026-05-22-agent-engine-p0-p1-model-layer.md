# 代码解读 Agent 引擎 — Plan A：模型层（Phase 0-1）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 LLM provider 层补上「流式 thinking 增量」能力，并先以一个 spike 验证 MiniMax-M2 是否支持 OpenAI function-calling —— 这是整个 agent 引擎的模型层地基。

**Architecture:** 后端已有 `complete_stream_with_tools()` 流式接口（yield `StreamTextDelta` + `ToolCall`）。本计划新增 `StreamThinkingDelta` 事件类型，让 DashScope（qwen-plus）把 `reasoning_content` 字段作为 thinking 流式吐出。MiniMax 的 `<think>` 提取 + 工具调用路径**故意不在本计划**——它的设计取决于 Task 1 de-risk 的结论（MiniMax 到底支不支持 tools），定了再写 Plan A-cont。

**Tech Stack:** Python 3.12 / httpx 流式 / DashScope OpenAI 兼容端点 / pytest（仓库 venv：`./venv/bin/python -m pytest`，homebrew python3 无 pytest-asyncio）。

**设计来源（单一真相）:** Obsidian `[[代码解读Agent引擎-设计]]` §4（双模型统一接口）+ §5（thinking 灰字）。

**范围边界:** 仅模型层 thinking 增量 + MiniMax de-risk。**不含**：agent loop 改造（Phase 2）、新工具（Phase 3）、SSE 事件（Phase 4-6）、前端、`KE_QA_USE_REACT` 切换。

**仓库 / 分支:** `/Users/java/knowledge-engineering-auth`，分支 `release-0513`（本会话后端长期分支；逐任务 commit 已授权，直接在分支干，无 worktree）。

---

## Task 1: MiniMax function-calling 兼容性 de-risk（spike，非 TDD）

**性质:** 这是一次验证性 spike，目的是回答「MiniMax-M2 的 OpenAI 兼容端点能否吃 `tools` 参数并返回 `tool_calls`」。结论决定 Plan A-cont 里 MiniMax 工具路径走「原生 function-calling」还是「提示词模拟」。**不写进生产代码**，跑完记录结论即可。

**Files:**
- Create: `scripts/spike_minimax_function_calling.py`（临时脚本，验证完可删）

- [ ] **Step 1: 写验证脚本**

```python
# scripts/spike_minimax_function_calling.py
"""
一次性 spike：验证 MiniMax-M2 OpenAI 兼容端点是否支持 function-calling。

跑法：./venv/bin/python scripts/spike_minimax_function_calling.py
读 .env.local 里的 MINIMAX_API_KEY / MINIMAX_BASE_URL / MINIMAX_MODEL。

判定：
  - 若响应里 choices[0].message.tool_calls 非空 → MiniMax 支持原生 function-calling ✅
  - 若报 4xx / 忽略 tools 字段直接文本回答 → 不支持，需提示词模拟降级 ❌
"""
import asyncio
import json
import os

import httpx
from dotenv import load_dotenv

# 显式路径加载 .env.local（python -c 风格 find_dotenv 在脚本里不稳）
load_dotenv("/Users/java/knowledge-engineering-auth/.env.local")


async def main() -> None:
    api_key = os.environ["MINIMAX_API_KEY"]
    base_url = os.getenv("MINIMAX_BASE_URL", "https://api.minimaxi.com/v1")
    model = os.getenv("MINIMAX_MODEL", "MiniMax-M2")

    # 最小 tools 定义：一个假的 get_weather，看模型会不会"决定调用"它
    tools = [{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询某城市天气",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string", "description": "城市名"}},
                "required": ["city"],
            },
        },
    }]
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "北京今天天气怎么样？"}],
        "tools": tools,
        "tool_choice": "auto",
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        print(f"HTTP {resp.status_code}")
        try:
            obj = resp.json()
        except Exception:
            print("非 JSON 响应:", resp.text[:500])
            return
        # 打印关键字段
        choices = obj.get("choices") or []
        if not choices:
            print("无 choices；原始响应:", json.dumps(obj, ensure_ascii=False)[:500])
            return
        message = choices[0].get("message", {})
        tool_calls = message.get("tool_calls")
        print("=== 判定 ===")
        if tool_calls:
            print("✅ 支持原生 function-calling；tool_calls =")
            print(json.dumps(tool_calls, ensure_ascii=False, indent=2))
        else:
            print("❌ 未返回 tool_calls（content 直答）→ 需提示词模拟降级")
            print("content:", (message.get("content") or "")[:300])


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: 跑脚本，记录结论**

Run: `./venv/bin/python scripts/spike_minimax_function_calling.py`

Expected: 打印 `HTTP 200` + 「✅ 支持」或「❌ 不支持」二选一。

> ⚠️ 需要先开 MySQL 隧道？**不需要**——这个脚本只打 MiniMax HTTP API，不碰数据库。但需要 `.env.local` 里有有效 `MINIMAX_API_KEY`。

- [ ] **Step 3: 把结论写进 Obsidian 设计文档 §4.2**

把判定结果（✅/❌ + tool_calls 样例 or 报错）追加到 `[[代码解读Agent引擎-设计]]` §4.2 的 de-risk 小节，供 Plan A-cont 决定 MiniMax 路径。**不 commit 脚本本身**（一次性 spike）。

- [ ] **Step 4: 删除 spike 脚本**

```bash
rm scripts/spike_minimax_function_calling.py
```

> 本 Task 无代码 commit（结论落 Obsidian）。如团队想留脚本做回归，可单独 commit 到 `scripts/`，但默认删除保持仓库干净。

---

## Task 2: 新增 `StreamThinkingDelta` 事件类型

**目标:** 给流式响应加一个「思考增量」事件类型，与现有 `StreamTextDelta`（答案文本）区分开。这是 thinking 灰字（§5）的数据契约。

**Files:**
- Modify: `src/service/qa_engine/llm_types.py`（在 `StreamTextDelta` 之后追加）
- Test: `tests/test_auth/test_qa_stream_thinking.py`（新建）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_auth/test_qa_stream_thinking.py
"""
StreamThinkingDelta：流式 thinking 增量类型（设计 §5）。
与 StreamTextDelta 区分——前端据此把 thinking 渲染成灰字。
"""
from src.service.qa_engine.llm_types import StreamThinkingDelta, StreamTextDelta


def test_thinking_delta_holds_text():
    # 思考增量携带一段推理文本
    d = StreamThinkingDelta(text="我需要先查 OrderService 的调用方")
    assert d.text == "我需要先查 OrderService 的调用方"


def test_thinking_delta_is_distinct_type_from_text_delta():
    # 类型必须可区分——上层 isinstance 分流到不同 SSE 通道
    t = StreamThinkingDelta(text="思考")
    a = StreamTextDelta(text="答案")
    assert not isinstance(t, StreamTextDelta)
    assert not isinstance(a, StreamThinkingDelta)


def test_thinking_delta_is_frozen():
    # frozen dataclass：构造后不可改（与 StreamTextDelta 同约束）
    import dataclasses
    d = StreamThinkingDelta(text="x")
    try:
        d.text = "y"  # type: ignore[misc]
        assert False, "应抛 FrozenInstanceError"
    except dataclasses.FrozenInstanceError:
        pass
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_stream_thinking.py -v`
Expected: FAIL，`ImportError: cannot import name 'StreamThinkingDelta'`

- [ ] **Step 3: 实现类型**

在 `src/service/qa_engine/llm_types.py` 末尾（`StreamTextDelta` 类定义之后）追加：

```python


@dataclass(frozen=True, slots=True)
class StreamThinkingDelta:
    """LLM 流式响应里的一段"思考"增量（区别于答案正文 StreamTextDelta）。

    来源：
      - DashScope/qwen：delta.reasoning_content 字段
      - MiniMax-M2：content 里的 <think>...</think> 段（Plan A-cont 处理）

    上层 sse_emitter 按 isinstance 把它路由到 SSE `thinking` 事件 → 前端灰字渲染。
    """
    text: str
```

- [ ] **Step 4: 跑测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_stream_thinking.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: commit**

```bash
git add src/service/qa_engine/llm_types.py tests/test_auth/test_qa_stream_thinking.py
git commit -m "$(cat <<'EOF'
feat(qa-engine): 新增 StreamThinkingDelta 流式思考增量类型

agent 引擎 Plan A Phase 1：与 StreamTextDelta（答案正文）区分的思考事件类型，
作为 thinking 灰字（设计 §5）的数据契约。DashScope reasoning_content / MiniMax <think>
都归一化成它，上层 sse_emitter 按 isinstance 路由到 SSE thinking 通道。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: DashScope 把 `reasoning_content` 作为 thinking 流式吐出

**目标:** qwen-plus 推理模型在流式响应里通过 `delta.reasoning_content` 给思考链。让 `_process_stream_line` 识别它并 yield `StreamThinkingDelta`，与答案正文（`delta.content` → `StreamTextDelta`）并行。

**Files:**
- Modify: `src/service/qa_engine/llm_dashscope.py:293-296`（`_process_stream_line` 内，文本增量处理之后插入 thinking 处理）
- Modify: `src/service/qa_engine/llm_dashscope.py`（顶部 import 加 `StreamThinkingDelta`）
- Test: `tests/test_auth/test_qa_dashscope_stream_tools.py`（追加 thinking 用例；该文件已存在）

- [ ] **Step 1: 写失败测试**

在 `tests/test_auth/test_qa_dashscope_stream_tools.py` 末尾追加：

```python
def test_process_stream_line_yields_thinking_from_reasoning_content():
    """qwen 推理模型：delta.reasoning_content → StreamThinkingDelta。"""
    from src.service.qa_engine.llm_dashscope import DashScopeProvider
    from src.service.qa_engine.llm_types import StreamThinkingDelta

    pending: dict = {}
    # 模拟一行带 reasoning_content 的 SSE
    line = 'data: {"choices":[{"delta":{"reasoning_content":"先看调用方"}}]}'
    events = DashScopeProvider._process_stream_line(line, pending)

    assert len(events) == 1
    assert isinstance(events[0], StreamThinkingDelta)
    assert events[0].text == "先看调用方"


def test_process_stream_line_thinking_and_content_both_present():
    """同一 delta 同时有 reasoning_content + content → 各 yield 一个事件，类型不同。"""
    from src.service.qa_engine.llm_dashscope import DashScopeProvider
    from src.service.qa_engine.llm_types import StreamThinkingDelta, StreamTextDelta

    pending: dict = {}
    line = 'data: {"choices":[{"delta":{"reasoning_content":"想","content":"答"}}]}'
    events = DashScopeProvider._process_stream_line(line, pending)

    # 思考在前、正文在后（顺序对前端渲染无强约束，但断言确定性）
    assert any(isinstance(e, StreamThinkingDelta) and e.text == "想" for e in events)
    assert any(isinstance(e, StreamTextDelta) and e.text == "答" for e in events)
    assert len(events) == 2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_dashscope_stream_tools.py -k thinking -v`
Expected: FAIL，`ImportError: cannot import name 'StreamThinkingDelta'`（dashscope 模块还没 import 它）或断言失败（没 yield thinking 事件）

- [ ] **Step 3: 实现 — import + 插入 reasoning_content 处理**

3a. 在 `src/service/qa_engine/llm_dashscope.py` 顶部 import 区，找到 `from src.service.qa_engine.llm_types import ...` 那行，把 `StreamThinkingDelta` 加进去。例如原本：

```python
from src.service.qa_engine.llm_types import LLMToolResponse, StreamTextDelta, ToolCall
```

改为：

```python
from src.service.qa_engine.llm_types import (
    LLMToolResponse,
    StreamTextDelta,
    StreamThinkingDelta,
    ToolCall,
)
```

3b. 在 `_process_stream_line` 里，文本增量处理（line 293-296）之后、tool_call 处理（line 298）之前，插入 thinking 处理：

```python
        # 1) 文本增量 → 立刻 yield
        content = delta.get("content")
        if content:
            events.append(StreamTextDelta(text=str(content)))

        # 1b) 思考增量（qwen 推理模型）→ yield StreamThinkingDelta（设计 §5）
        #     reasoning_content 是 DashScope 给推理模型流式思考链的专用字段；
        #     与 content（答案正文）并行，前端据类型渲染成灰字。
        reasoning = delta.get("reasoning_content")
        if reasoning:
            events.append(StreamThinkingDelta(text=str(reasoning)))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_dashscope_stream_tools.py -v`
Expected: PASS（含原有用例 + 2 个新 thinking 用例，全过）

- [ ] **Step 5: commit**

```bash
git add src/service/qa_engine/llm_dashscope.py tests/test_auth/test_qa_dashscope_stream_tools.py
git commit -m "$(cat <<'EOF'
feat(qa-engine): DashScope 流式吐 reasoning_content 作为 StreamThinkingDelta

agent 引擎 Plan A Phase 1：qwen-plus 推理模型的 delta.reasoning_content 字段
归一化为 StreamThinkingDelta，与答案正文 StreamTextDelta 并行流式。这是 thinking
灰字（设计 §5）在 qwen 侧的落地。MiniMax <think> 提取见 Plan A-cont（取决于 Task 1 de-risk）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: 模型层回归 + 收尾

**目标:** 确认 Phase 1 改动不回归既有流式/工具测试。

- [ ] **Step 1: 跑 qa_engine 相关测试全集**

Run: `./venv/bin/python -m pytest tests/test_auth/ -k "qa or stream or dashscope or react" -v`
Expected: 全 PASS（含 test_qa_dashscope / test_qa_dashscope_stream_tools / test_qa_react_* / test_qa_stream_thinking）

- [ ] **Step 2: import 自检**

Run: `./venv/bin/python -c "from src.service.qa_engine.llm_types import StreamThinkingDelta, StreamTextDelta, ToolCall, LLMToolResponse; from src.service.qa_engine.llm_dashscope import DashScopeProvider; print('OK')"`
Expected: 打印 `OK`，无 ImportError

- [ ] **Step 3: 更新 Obsidian 设计文档进度**

在 `[[代码解读Agent引擎-设计]]` §11 开发规划表，把 Phase 0 / Phase 1（DashScope 侧）标记为「✅ 已实现」，并注明 MiniMax thinking+tools 待 Plan A-cont（依赖 Task 1 de-risk 结论）。

---

## Plan A 完成定义（验收）

1. ✅ Task 1 spike 已跑，MiniMax function-calling 支持与否的结论已写进设计 §4.2
2. ✅ `StreamThinkingDelta` 类型存在、frozen、与 `StreamTextDelta` 可区分
3. ✅ qwen-plus 流式响应中 `reasoning_content` → `StreamThinkingDelta`，与 `content` 并行
4. ✅ `tests/test_auth/` 中 qa/stream/dashscope/react 相关测试全过，无回归
5. ✅ 设计文档 §11 进度更新

## 后续计划（不在本 Plan）

- **Plan A-cont**：MiniMax `<think>` 提取 + 工具路径（原生 or 提示词模拟，取决于 Task 1）
- **Plan B**：Phase 2 agent loop 改造（cap 3→12 + 停止条件 + per-request registry）+ Phase 3 三个新工具（ke_read_entity / ke_impact / ke_method_interp，含 GraphProto + Neo4jGraphAdapter 扩展 impact_closure / get_node）
- **Plan C**：Phase 4-6 SSE 事件（thinking/todo/citation）+ 前端组件 + Phase 7 自由格式 + 开关上线 + Phase 8 回归
