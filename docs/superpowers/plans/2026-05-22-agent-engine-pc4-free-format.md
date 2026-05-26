# 代码解读 Agent 引擎 — Plan C4：自由格式输出（chat/agent 路径）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 ReAct agent（chat 路径）用**自然 markdown** 作答，而不是被强制塞进 6 段式 JSON——保留 §6 反幻觉约束（只基于真实 entity 作答 + `[entity_id|显示文本]` 引用标记）和 Mermaid 约定，去掉"必须 6 段 JSON"的硬结构。结构化 6 段式能力保留给非 chat 的"结构化技术解读"模式（QASynthesizer 不动）。

**Architecture:** 新增 `AGENT_SYSTEM_PROMPT`（SYSTEM_PROMPT 的自由格式变体）+ 给 `build_user_prompt` 加 `free_format` 开关（只替换尾部【任务】指令块，context 拼装部分照旧 DRY 复用）。`ReActSynthesizer.synthesize`/`synthesize_stream` 改用这两者。模型输出自然 markdown → 既有 `QASynthesizer._parse_sections` 的**降级分支**（非 JSON → 包成单段 `overview`，synthesizer.py:356）自动接住，sse_emitter 的 `token` 流 + section dump 照常工作，前端已支持 markdown/代码高亮（§7）。**零改 sse_emitter / _parse_sections。**

**Tech Stack:** Python 3.12 / pytest（仓库 venv：`./venv/bin/python -m pytest`）。

**设计来源:** Obsidian `[[代码解读Agent引擎-设计]]` §7（自由格式输出）+ §11 Phase 7。

**前置:** Plan A→C3 已落地（8 工具 + meta todo_write + loop + thinking/citations/todo SSE 三件套 + memory_block 注入）。

**范围边界（重要）:**
- ✅ 本 Plan：chat/agent（ReAct）路径自由格式输出。
- ❌ **不含** `KE_QA_USE_REACT 默认翻 ON`（把 agent 对所有用户上线）——这是独立的上线/产品决策，用户明确要求**单独议、本 Plan 不动 api.py:168**。
- ❌ 不含前端渲染（Plan C-frontend）。
- ❌ 不动 QASynthesizer / `_parse_sections` / sse_emitter。

**关键现状（已确认）:**
- `SYSTEM_PROMPT`（`prompts.py:23-126`）：强制 6 段式 JSON（Step 2.2「必须按 6 段式 JSON」、Step 3「6 段式结构」、Step 5「必须合法 JSON ```json fenced」）。含反幻觉规则（Step 2.1 不编造）、引用标记 `[entity_id|显示文本]`（Step 2.5）、Mermaid 约定（Step 4）、视角选择（Step 1）。
- `build_user_prompt(question, context)`（`prompts.py:142-232`）：拼 context（候选/调用关系/db）+ 尾部【任务】块（line 224-230）强制「组织 6 段式答案」「严格按 JSON 输出」。context 拼装部分（line 154-221）与输出格式无关，可复用。
- `_parse_sections`（`synthesizer.py:307-364`）：解析 ```json fence / 裸 JSON；**都失败 → 降级包成单段** `{"type":"overview","title":"回答","content":raw,"references":[]}`（line 356-363）。所以自由格式 markdown 输出会被自动接住。
- `ReActSynthesizer`（`react_synthesizer.py`）两方法 `synthesize`（~line 87-101）/`synthesize_stream`（~line 210-222）当前：
  ```python
  from src.service.qa_engine.prompts import SYSTEM_PROMPT, build_user_prompt, with_memory_block
  ...
  user_prompt = build_user_prompt(ctx.question, _ctx_to_dict(ctx))
  base_system = with_memory_block(SYSTEM_PROMPT, memory_block)
  system_text = base_system
  tool_hint = self._build_tool_usage_hint()
  if tool_hint:
      system_text = f"{base_system}\n\n{tool_hint}"
  ```
  （`with_memory_block` 是 C3 Task1 加的；`_build_tool_usage_hint` 已含 entity_id 反编造约束。）
- `QASynthesizer`（`synthesizer.py:100`）继续用 `SYSTEM_PROMPT` + `build_user_prompt`（6 段式）——本 Plan **不动**。
- 既有 react 单测用 `_ok_answer_json()`（合法 6 段 JSON）当 fake LLM 输出 → 改 prompt **不影响** fake 返回值，`_parse_sections` 照常解析，故既有断言不破。`test_react_system_prompt_lists_available_tools` 断言 sys_text 含 `ke_search`/`entity_id` —— tool 名来自 `_build_tool_usage_hint`（appended，不动），`entity_id` 同样在 tool_hint 里，故仍成立。

**仓库 / 分支:** `/Users/java/knowledge-engineering-auth`，分支 `release-0513`。

---

## Task 1: `AGENT_SYSTEM_PROMPT` + `build_user_prompt(free_format=)`

**Files:**
- Modify: `src/service/qa_engine/prompts.py`（加 AGENT_SYSTEM_PROMPT 常量 + build_user_prompt 加 free_format 参数）
- Test: Create `tests/test_auth/test_qa_free_format_prompt.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_auth/test_qa_free_format_prompt.py`：

```python
"""自由格式 prompt（设计 §7）：AGENT_SYSTEM_PROMPT 不强制 6 段 JSON，但保留反幻觉 +
引用标记；build_user_prompt(free_format=True) 尾部任务块不再要求 JSON。"""
from src.service.qa_engine.prompts import (
    AGENT_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_user_prompt,
)


def test_agent_system_prompt_is_free_format():
    """AGENT_SYSTEM_PROMPT 去掉 6 段 JSON 强制，保留反幻觉 + 引用标记 + markdown 指引。"""
    # 不再强制"6 段式 JSON / 必须合法 JSON"
    assert "6 段式 JSON" not in AGENT_SYSTEM_PROMPT
    assert "必须合法 JSON" not in AGENT_SYSTEM_PROMPT
    # 自由格式：markdown
    assert "markdown" in AGENT_SYSTEM_PROMPT.lower()
    # 保留反幻觉（§6）+ 引用标记（前端转链接）
    assert "不允许编造" in AGENT_SYSTEM_PROMPT or "不能编造" in AGENT_SYSTEM_PROMPT
    assert "[entity_id|显示文本]" in AGENT_SYSTEM_PROMPT
    # 与结构化 SYSTEM_PROMPT 不是同一个
    assert AGENT_SYSTEM_PROMPT != SYSTEM_PROMPT


def test_build_user_prompt_free_format_drops_json_instruction():
    """free_format=True 时尾部任务块不再要求 6 段 / JSON。"""
    ctx = {"entry_candidates": [{"entity_id": "method//A", "summary_text": "x", "level": "api"}]}
    free = build_user_prompt("q", ctx, free_format=True)
    structured = build_user_prompt("q", ctx)

    # 自由格式：不提 6 段 / JSON
    assert "6 段式" not in free
    assert "严格按 JSON 输出" not in free
    # 自由格式仍带 markdown 指引
    assert "markdown" in free.lower()
    # 结构化（默认）保持原样：仍要求 6 段 + JSON
    assert "6 段式" in structured
    assert "严格按 JSON 输出" in structured
    # context 拼装部分两者都在（DRY 复用）
    assert "method//A" in free and "method//A" in structured


def test_build_user_prompt_default_is_structured():
    """不传 free_format 默认 False（向后兼容，QASynthesizer 仍走 6 段）。"""
    ctx = {"entry_candidates": []}
    assert "6 段式" in build_user_prompt("q", ctx)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_free_format_prompt.py -v`
Expected: FAIL —— `ImportError: cannot import name 'AGENT_SYSTEM_PROMPT'`。

- [ ] **Step 3a: 加 AGENT_SYSTEM_PROMPT 常量**

`src/service/qa_engine/prompts.py`，在 `SYSTEM_PROMPT = """..."""`（line 126 结束）之后加：

```python
# ─── 自由格式 system prompt（v1.4 / Plan C4，设计 §7）────────────────────────
# chat/agent（ReAct）路径用：保留分析师角色 + 视角 + 反幻觉 + 引用标记 + Mermaid 约定，
# 但**不**强制 6 段式 JSON——模型自然 markdown 作答。结构化 6 段能力保留在 SYSTEM_PROMPT
# 给"结构化技术解读"非 chat 场景。输出由 _parse_sections 降级分支接住（非 JSON → 单段）。
AGENT_SYSTEM_PROMPT = """你是企业代码知识分析师。你的任务是把代码翻译成业务方/新人能读懂的业务说明，并直接回答用户的问题。

【作答风格】
- 用**自然的 markdown** 作答：按需用标题、列表、表格、代码块（```lang）组织，不必套固定结构。
- 简洁专业、中文；篇幅与问题复杂度匹配，不啰嗦也不凑字数。
- 调用链/架构/数据流等适合图的，按下方 Mermaid 约定画图。

【严格规则】
1. **不允许编造**：所有方法名、类名、表名必须出自我提供的 context 或工具返回结果，不能从你的知识里"想当然"；宁可说"未找到"也不要虚构 entity_id / 代码内容。
2. **引用标记**：提到方法/类/表时，用 `[entity_id|显示文本]` 格式（前端会转成可点击链接），例：`[method://com.bank.openAccount|DepositController.openAccount()]`。
3. **视角**（可选锚定）：先想清楚用户要的是"整体架构 / 请求流程 / 数据流 / 依赖关系 / 业务规则 / 外部集成"哪一类，据此组织重点，但不必显式声明视角。
4. context 不足以回答时：直接说明"未找到相关业务逻辑，建议换个说法"，不要硬编。

【Mermaid 约定（画图时遵守）】
- 节点 ID 必须用 context 给出的 entity_id，不能编造。
- 节点标签两行：显示名 + `\\n` + 真实路径，例：`open-account["OpenAccount\\nsrc/deposit/OpenAccount.java"]`。
- 边必带语义标签：`A -->|"调用 / 写入 / 校验"| B`。
- 节点超过 5 个时拆图，避免毛球图。
- 4 类预设样式：external `#585b70`（外部系统）/ entry `#89b4fa`（入口）/ store `#a6e3a1`（持久化）/ concern `#f38ba8`（风险）。
- Mermaid 写在 ` ```mermaid ` fenced code block 里。
"""
```

- [ ] **Step 3b: build_user_prompt 加 free_format 参数**

`prompts.py` 的 `build_user_prompt`（line 142）签名加 `free_format`：

```python
def build_user_prompt(question: str, context: dict[str, Any], free_format: bool = False) -> str:
```

把尾部「4. 任务指令」块（line 223-230）改成按 free_format 分叉（context 拼装 line 154-221 **不动**）：

```python
    # 4. 任务指令
    parts.append("")
    parts.append("【任务】")
    parts.append("基于以上 context 回答用户问题。")
    if free_format:
        # 自由格式（chat/agent，设计 §7）：自然 markdown，不套 6 段
        parts.append("用自然的 markdown 作答（标题/列表/代码块按需），不必套固定结构。")
        parts.append("提到方法/类/表时用 `[entity_id|显示文本]` 标注；只能基于 context/工具返回的真实实体，不得编造 entity_id。")
        parts.append("如果 context 不足以回答，直接说明未找到并建议换个说法。")
    else:
        # 结构化 6 段（非 chat / QASynthesizer，保持原样）
        parts.append("先按 system prompt 里的 Step 1 选 1 个主视角，再按该视角侧重组织 6 段式答案。")
        parts.append("严格按 JSON 输出，缺信息段跳过；overview 段无论如何都要出（注明视角）。")
        parts.append("如果 context 不足以回答（比如候选都不相关），")
        parts.append("仍要给一个 overview 段说明：视角：overall-architecture\\n未找到相关业务逻辑，建议换个说法。")

    return "\n".join(parts)
```

> 读文件确认 line 223-230 的确切文本后整体替换 if/else；保留 152-221 的 context 拼装一字不动。

- [ ] **Step 4: 跑测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_free_format_prompt.py -v`
Expected: PASS（3 用例）

- [ ] **Step 5: commit**

```bash
git add src/service/qa_engine/prompts.py tests/test_auth/test_qa_free_format_prompt.py
git commit -m "$(cat <<'EOF'
feat(qa-engine): 加 AGENT_SYSTEM_PROMPT 自由格式 + build_user_prompt free_format 开关

agent 引擎 Plan C4 Phase 7：新增自由格式 system prompt（保留反幻觉 + [entity_id|文本]
引用标记 + Mermaid 约定，去掉 6 段 JSON 强制）；build_user_prompt 加 free_format 开关
只换尾部任务块。结构化 6 段（SYSTEM_PROMPT / QASynthesizer）不动。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: ReActSynthesizer 用自由格式 prompt

**Files:**
- Modify: `src/service/qa_engine/react_synthesizer.py`（synthesize + synthesize_stream）
- Test: Modify `tests/test_auth/test_qa_react_synthesizer.py`（追加自由格式断言）

- [ ] **Step 1: 写失败测试**

在 `tests/test_auth/test_qa_react_synthesizer.py` 末尾追加：

```python
@pytest.mark.asyncio
async def test_react_uses_free_format_system_prompt():
    """ReActSynthesizer 走自由格式 prompt（AGENT_SYSTEM_PROMPT），system 消息不再强制 6 段 JSON。"""
    from unittest.mock import AsyncMock
    from src.service.qa_engine.react_synthesizer import ReActSynthesizer
    from src.service.qa_engine.tools.base import ToolRegistry
    from src.service.qa_engine.llm_types import LLMToolResponse
    from src.service.qa_engine.retriever import RetrievedContext

    llm = AsyncMock()
    llm.complete_with_tools = AsyncMock(return_value=LLMToolResponse(
        content="## 概述\n这是自由格式答案", tool_calls=[],
    ))
    synth = ReActSynthesizer(llm_provider=llm, tool_registry=ToolRegistry(), max_iterations=3)
    ctx = RetrievedContext(question="VetController 调了谁？", project_id="p")
    answer = await synth.synthesize(ctx, history=[])

    sys_text = llm.complete_with_tools.call_args.kwargs["messages"][0]["content"]
    # 自由格式：不强制 6 段 JSON
    assert "6 段式 JSON" not in sys_text
    assert "必须合法 JSON" not in sys_text
    assert "markdown" in sys_text.lower()
    # 反幻觉保留
    assert "不允许编造" in sys_text or "不能编造" in sys_text
    # 非 JSON markdown 输出被 _parse_sections 降级接住成单段
    assert answer.sections and answer.sections[0]["content"] == "## 概述\n这是自由格式答案"


def test_qa_synthesizer_still_uses_structured_prompt():
    """回归：QASynthesizer（非 chat）仍用 6 段式 SYSTEM_PROMPT，不受 C4 影响。"""
    from src.service.qa_engine.prompts import SYSTEM_PROMPT
    assert "6 段式" in SYSTEM_PROMPT  # 结构化 prompt 未被改动
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_react_synthesizer.py::test_react_uses_free_format_system_prompt -v`
Expected: FAIL —— sys_text 仍是 SYSTEM_PROMPT（含"6 段式 JSON"），断言不成立。

- [ ] **Step 3: synthesize + synthesize_stream 改用自由格式 prompt**

`react_synthesizer.py` 两方法（`synthesize` ~line 87-101、`synthesize_stream` ~line 210-222）：
1. 方法体开头 import 把 `SYSTEM_PROMPT` 换成 `AGENT_SYSTEM_PROMPT`：
   ```python
   from src.service.qa_engine.prompts import AGENT_SYSTEM_PROMPT, build_user_prompt, with_memory_block
   ```
2. user_prompt 传 `free_format=True`：
   ```python
   user_prompt = build_user_prompt(ctx.question, _ctx_to_dict(ctx), free_format=True)
   ```
3. base_system 用 AGENT_SYSTEM_PROMPT：
   ```python
   base_system = with_memory_block(AGENT_SYSTEM_PROMPT, memory_block)
   system_text = base_system
   tool_hint = self._build_tool_usage_hint()
   if tool_hint:
       system_text = f"{base_system}\n\n{tool_hint}"
   ```
   （tool_hint 拼接逻辑不动；两方法对称改。）

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_react_synthesizer.py -v`
Expected: PASS（新 2 用例 + 既有全过。既有用例 fake LLM 返 `_ok_answer_json()` 合法 6 段 JSON，`_parse_sections` 照常解析，prompt 变化不影响这些断言；`test_react_system_prompt_lists_available_tools` 的 `ke_search`/`entity_id` 来自 tool_hint 仍在）。

- [ ] **Step 5: commit**

```bash
git add src/service/qa_engine/react_synthesizer.py tests/test_auth/test_qa_react_synthesizer.py
git commit -m "$(cat <<'EOF'
feat(qa-engine): ReActSynthesizer 改用自由格式 prompt（chat 路径放开 6 段）

agent 引擎 Plan C4 Phase 7：synthesize/synthesize_stream 改用 AGENT_SYSTEM_PROMPT +
build_user_prompt(free_format=True)，agent 自然 markdown 作答；非 JSON 输出由
_parse_sections 降级分支接住成单段。QASynthesizer 结构化 6 段不变。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: 回归 + 设计文档

**Files:**
- Test: 无新增（跑全量相关回归）
- Doc: Obsidian `[[代码解读Agent引擎-设计]]` §7 + §11 Phase 7（controller 用真实 SHA 更新）

- [ ] **Step 1: 全量相关回归**

Run: `./venv/bin/python -m pytest tests/test_auth/ -k "qa or sse or stream or react or think or synthes or tool or todo or registry or prompt or free_format" -q`
Expected: 全 PASS（重点确认既有 react / sse / synthesizer 测试不被 prompt 改动破坏）

- [ ] **Step 2: import + 行为自检**

Run: `./venv/bin/python -c "from src.service.qa_engine.prompts import AGENT_SYSTEM_PROMPT, build_user_prompt; print('6 段式 JSON' not in AGENT_SYSTEM_PROMPT); print('严格按 JSON 输出' not in build_user_prompt('q', {}, free_format=True)); print('严格按 JSON 输出' in build_user_prompt('q', {}))"`
Expected: 三行均 `True`

- [ ] **Step 3: 更新设计 §7 + §11 Phase 7（controller）**

`[[代码解读Agent引擎-设计]]`：
- §7 自由格式输出：标注后端已落地（Plan C4，commit refs）。
- §11 Phase 7：标「🔵 后端自由格式完成（Plan C4，commit refs）；`KE_QA_USE_REACT` 默认 ON 的上线翻转**单独决策、未做**」。

> 设计文档由 controller 用真实 SHA 更新，executor 不动 Obsidian。

---

## Plan C4 完成定义（验收）

1. ✅ `AGENT_SYSTEM_PROMPT`：自由格式（无 6 段 JSON 强制）、保留反幻觉 + `[entity_id|显示文本]` 引用 + Mermaid（单测覆盖）
2. ✅ `build_user_prompt(free_format=True)` 换尾部任务块、默认 False 向后兼容（单测覆盖）
3. ✅ `ReActSynthesizer` 两方法改用 AGENT_SYSTEM_PROMPT + free_format user prompt；自然 markdown 输出被 `_parse_sections` 降级接住（单测覆盖）
4. ✅ QASynthesizer / `_parse_sections` / sse_emitter **零改动**，6 段结构化能力保留
5. ✅ qa/sse/stream/react/synthes/tool/todo/registry/prompt 测试全过
6. ✅ 设计 §7 + §11 Phase 7 后端标记（含"上线开关未翻"说明）

## 后续计划（不在本 Plan）
- **上线开关**：`KE_QA_USE_REACT` 默认翻 ON（api.py:168）—— 独立产品/上线决策，用户拍板后单独做。
- **Plan C-frontend**：前端渲染 thinking 灰字（C1）+ citations 引用（C2）+ todo checklist（C3）+ 自由格式 markdown（C4）。
