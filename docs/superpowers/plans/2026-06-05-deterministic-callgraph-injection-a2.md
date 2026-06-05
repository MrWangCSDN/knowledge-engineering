# A2 确定性调用图注入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 架构类问题（`skill_id=="architecture"` 且有调用边）由系统**确定性注入**一张 reactflow 调用图，不再依赖 agent 自觉调 `render_call_graph`（治"有时不出图"）。

**Architecture:** 在 `sse_emitter.stream_qa_answer` 召回后、正文流式前，门控命中则用 `render_call_graph` 的渲染核心 `_build_call_chain_section_from_edges` 确定性构图，组装成现有的 `tool_call`+`render` SSE 事件（at=0）压进 `pending_tool_events`。复用现有 SSE render 路径 + 前端 `at` 内联 + 去重，**前端零改**。

**Tech Stack:** Python 3.11 / pytest。设计见 Obsidian [[业务问答-确定性调用图注入-A2设计]]。

**约束**：TDD、frequent commits、Python 中文逐行注释；不动 6 段路径 / 召回门控 / `render_call_graph` 工具本身 / 前端；设计文档 Obsidian 不双写；部署 git bundle + eval gate（50 题）属部署后步骤，需用户授权、本计划不自动执行。

---

## 现状关键事实（实现前必读）

- `sse_emitter.stream_qa_answer`（sse_emitter.py）：`ctx = await retriever.retrieve(...)`（L148，try/except 包裹）→ yield route/step 事件 → `pending_tool_events: list = []`（L175）+ `_offset = [0]`（L178 旁）→ `_on_tool_call`（L182+，payload 形态 `{phase,id,name,at,arguments|result_preview,render?}`）→ stream 循环里 `while pending_tool_events:` flush（约 L275+）。**注入点 = `pending_tool_events`/`_offset` 声明之后、stream 循环之前。**
- `synthesizer._build_call_chain_section_from_edges(call_edges_by_entry, max_nodes=_CALLCHAIN_MAX_NODES, node_summaries=None) -> dict | None`：返 `{"type":"call_chain","title":"调用链路","content": <JSON 字符串 {nodes,edges}>}`；空/全噪声/截断后无边 → None。节点已含 `method` 中英双语字段（B3 `fe6a06e`）。
- `ctx.skill_id` 仅 `"architecture"`(KE 命中) / `"chit-chat"`(低召回)（retriever.py:186/192）。`ctx.call_edges_by_entry`（dict, entry→[(from,to),...]）、`ctx.callchain_node_summaries`（dict, id→中文解读）。
- 前端 `chat.ts` tool_call complete 分支：`payload.render!=null` → 存 `render` + `at`（已优先用 `payload.at`，`107ab77`）→ `buildAnswerSegments` 按 at 内联 → CallChainFlow 渲染。合成事件复用此路径，**前端零改**。
- `RetrievedContext`（retriever.py）：dataclass，字段含 `question`/`project_id`/`skill_id`/`call_edges_by_entry`/`callchain_node_summaries`，可直接构造作测试夹具。

---

## File Structure

- **Modify** `src/service/qa_engine/sse_emitter.py` — 加模块级 `_should_auto_render` + `build_auto_call_graph_event` + `stream_qa_answer` 注入接线。
- **Modify** `src/service/qa_engine/prompts.py` — `AGENT_SYSTEM_PROMPT` 作答风格/画图约定 + `build_user_prompt` free_format 分支：告知"主图自动展示、无需调画图工具画主图"。
- **Test**: `tests/test_auth/test_auto_callgraph_inject.py`（门控 + 构建 + 注入）、扩 `tests/test_auth/test_diagram_render_tool_only.py`（prompt 不变量）。

---

## Task 1: 门控纯函数 + 合成事件构建

**Files:** Modify `src/service/qa_engine/sse_emitter.py`；Test `tests/test_auth/test_auto_callgraph_inject.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_auth/test_auto_callgraph_inject.py
"""A2 确定性调用图注入：门控 + 合成事件构建 + sse 注入。设计 [[业务问答-确定性调用图注入-A2设计]]。"""
from src.service.qa_engine.sse_emitter import _should_auto_render, build_auto_call_graph_event
from src.service.qa_engine.retriever import RetrievedContext


def _arch_ctx():
    """架构题 ctx：skill=architecture + 有调用边 + 一条 2b 中文解读。"""
    return RetrievedContext(
        question="用户提交订单怎么生成订单", project_id="p", skill_id="architecture",
        call_edges_by_entry={"OmsPortalOrderServiceImpl::generateOrder": [
            ("OmsPortalOrderServiceImpl::generateOrder", "OmsPortalOrderServiceImpl::lockStock")]},
        callchain_node_summaries={"OmsPortalOrderServiceImpl::generateOrder": "生成订单主流程"},
    )


def test_gate_true_for_architecture_with_edges():
    assert _should_auto_render(_arch_ctx()) is True


def test_gate_false_for_chitchat():
    c = _arch_ctx(); c.skill_id = "chit-chat"
    assert _should_auto_render(c) is False


def test_gate_false_for_empty_edges():
    c = _arch_ctx(); c.call_edges_by_entry = {}
    assert _should_auto_render(c) is False


def test_build_event_when_gated():
    ev = build_auto_call_graph_event(_arch_ctx())
    assert ev is not None
    assert ev["name"] == "render_call_graph" and ev["phase"] == "complete" and ev["at"] == 0
    data = ev["render"]["data"]
    assert ev["render"]["kind"] == "call_graph" and len(data["nodes"]) >= 1
    # 节点中英双语（B3）：有 method 字段
    assert any(n.get("method") for n in data["nodes"])


def test_build_event_none_when_not_gated():
    c = _arch_ctx(); c.skill_id = "chit-chat"
    assert build_auto_call_graph_event(c) is None


def test_build_event_fail_soft_on_bad_ctx():
    class _Bad:  # 无 skill_id / call_edges_by_entry 属性 → 不崩、返 None
        pass
    assert build_auto_call_graph_event(_Bad()) is None
```

Run: `venv/bin/python -m pytest tests/test_auth/test_auto_callgraph_inject.py -q`
Expected: FAIL（`_should_auto_render` / `build_auto_call_graph_event` 不存在）

- [ ] **Step 2: 实现**（加到 sse_emitter.py 模块级，`format_sse` 之后、`stream_qa_answer` 之前）

```python
# ─── A2 确定性调用图注入（[[业务问答-确定性调用图注入-A2设计]]）────────────────
def _should_auto_render(ctx) -> bool:
    """门控：KE 命中（skill_id=architecture，非 chit-chat）且有调用边 → 自动出主图。

    chit-chat / 无调用边（纯查询/规则类）不出图。getattr 兜底旧 ctx/异常 ctx。
    """
    if getattr(ctx, "skill_id", "architecture") != "architecture":   # 闲聊路径不出图
        return False
    edges = getattr(ctx, "call_edges_by_entry", None) or {}          # 缺字段 → 空
    return any(edges.values())                                       # 任一入口有边


def build_auto_call_graph_event(ctx):
    """门控命中 → 用 render_call_graph 的渲染核心确定性构图，返回合成 tool_call 事件 payload；
    否则 / 构图为空 / 任何异常 → None（fail-soft，绝不阻断 agent 答题）。

    Returns: dict（{phase,id,name,render,at}）或 None。
    """
    if not _should_auto_render(ctx):
        return None
    try:
        # 复用 synthesizer 的确定性构图（= render_call_graph 工具的渲染核心，节点含 method 双语字段）
        from src.service.qa_engine.synthesizer import _build_call_chain_section_from_edges
        section = _build_call_chain_section_from_edges(
            ctx.call_edges_by_entry,
            node_summaries=getattr(ctx, "callchain_node_summaries", None),
        )
        if not section:                       # 全噪声 / 截断后无边 → 不注入
            return None
        data = json.loads(section["content"])  # section.content 是 {nodes,edges} 的 JSON 字符串
        if not data.get("nodes"):             # 双保险：无节点不注入
            return None
        # 合成一个"complete 阶段的 render 工具调用"事件，复用现有 tool_call+render SSE 路径；
        # at=0 → 前端 buildAnswerSegments 把图内联到回答最开头（graph-first）。
        return {
            "phase": "complete",
            "id": "auto_cg",                  # 固定 id，不与 LLM 的 tool_call id 冲突
            "name": "render_call_graph",      # 复用渲染类工具名（前端按此识别 render 段，不收敛进调查折叠）
            "render": {"kind": "call_graph", "data": data},
            "at": 0,
        }
    except Exception:                         # best-effort：构图/解析任何异常都不阻断主流程
        return None
```

确认 `import json` 已在 sse_emitter.py 顶部（`format_sse` 用了 `json.dumps`，已导入）。

Run: `venv/bin/python -m pytest tests/test_auth/test_auto_callgraph_inject.py -q` → PASS（6 例）

- [ ] **Step 3: Commit** — `git add -A && git commit -m "feat(qa): A2 门控 + 确定性调用图事件构建（render_call_graph 渲染核心复用，fail-soft）"`

---

## Task 2: sse_emitter 注入接线

**Files:** Modify `src/service/qa_engine/sse_emitter.py:175`旁；Test `tests/test_auth/test_auto_callgraph_inject.py`

- [ ] **Step 1: 写失败测试**（追加到 test_auto_callgraph_inject.py）

```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from src.service.qa_engine.sse_emitter import stream_qa_answer
from src.service.qa_engine.react_synthesizer import ReActSynthesizer
from src.service.qa_engine.synthesizer import SynthesizedAnswer


def _mock_retriever(ctx):
    r = MagicMock(); r.retrieve = AsyncMock(return_value=ctx)
    return r


def _mock_react_synth():
    synth = MagicMock(spec=ReActSynthesizer)
    final = SynthesizedAnswer(sections=[{"type": "overview", "title": "x", "content": "y", "references": []}],
                              token_usage=10, cost_yuan=0.0)
    async def fake_stream(ctx, history=None, on_token=None, on_tool_call=None, memory_block=None, **kw):
        if on_token:
            await on_token("订单生成流程：")
        return final
    synth.synthesize_stream = AsyncMock(side_effect=fake_stream)
    return synth


@pytest.mark.asyncio
async def test_sse_injects_auto_callgraph_for_architecture():
    """架构题(有边) → SSE body 含 tool_call render 事件且 at:0（确定性注入主图）。"""
    body = "".join([c async for c in stream_qa_answer(
        question="q", project_id="p", session_id="s",
        retriever=_mock_retriever(_arch_ctx()), synthesizer=_mock_react_synth())])
    assert "event: tool_call" in body
    import re, json as _json
    tc = re.findall(r"event: tool_call\ndata: (\{.*\})", body)
    assert any(_json.loads(b).get("name") == "render_call_graph" and _json.loads(b).get("at") == 0
               and _json.loads(b).get("render") for b in tc)


@pytest.mark.asyncio
async def test_sse_no_inject_for_chitchat():
    """chit-chat → 不注入自动图。"""
    c = _arch_ctx(); c.skill_id = "chit-chat"
    body = "".join([x async for x in stream_qa_answer(
        question="q", project_id="p", session_id="s",
        retriever=_mock_retriever(c), synthesizer=_mock_react_synth())])
    import re, json as _json
    tc = re.findall(r"event: tool_call\ndata: (\{.*\})", body)
    assert not any(_json.loads(b).get("id") == "auto_cg" for b in tc)
```

Run: `venv/bin/python -m pytest tests/test_auth/test_auto_callgraph_inject.py -q`
Expected: 注入两例 FAIL（尚未接线）

- [ ] **Step 2: 接线**（sse_emitter.py，`pending_tool_events`/`_offset` 声明之后、stream 主循环之前）

找到（约 L175-180）：
```python
    pending_tool_events: list[tuple[str, dict]] = []
    # 已 emit 的正文字符数（流式偏移）：...
    _offset = [0]
```
在其后插入：
```python
    # A2 确定性调用图注入（[[业务问答-确定性调用图注入-A2设计]]）：门控命中（架构题 + 有调用边）→
    # 系统确定性出主图、压进 pending_tool_events（at=0、正文流式之前 flush），不靠 agent 自觉调工具。
    _auto_cg = build_auto_call_graph_event(ctx)
    if _auto_cg is not None:
        pending_tool_events.append(("tool_call", _auto_cg))
```

Run: `venv/bin/python -m pytest tests/test_auth/test_auto_callgraph_inject.py -q` → PASS（8 例）

- [ ] **Step 3: Commit** — `git add -A && git commit -m "feat(qa): sse_emitter 召回后确定性注入调用图（at=0，门控命中）"`

---

## Task 3: prompt 微调（告知主图自动展示）

**Files:** Modify `src/service/qa_engine/prompts.py`；Test `tests/test_auth/test_diagram_render_tool_only.py`

- [ ] **Step 1: 写失败测试**（追加到 test_diagram_render_tool_only.py）

```python
def test_agent_prompt_says_main_graph_auto_shown():
    """A2：AGENT_SYSTEM_PROMPT 告知主调用图自动展示、无需自己调画图工具画主图。"""
    body = AGENT_SYSTEM_PROMPT
    assert "自动展示" in body
    assert "无需" in body and "画图工具" in body


def test_free_format_user_prompt_says_main_graph_auto_shown():
    """A2：free_format user prompt 也提示主图自动展示。"""
    from src.service.qa_engine.prompts import build_user_prompt
    p = build_user_prompt("q", {"entry_candidates": [], "skill_id": "architecture"}, free_format=True)
    assert "自动展示" in p
```

Run: `venv/bin/python -m pytest tests/test_auth/test_diagram_render_tool_only.py -q`
Expected: 2 例 FAIL

- [ ] **Step 2: 改 AGENT_SYSTEM_PROMPT 作答风格**（prompts.py）

找到（作答风格段，约 L179）：
```
- 调用链/架构/数据流等需要图时，**一律调 `render_call_graph` 工具出图**（见下【画图约定】），绝不自己手画。
```
替换为：
```
- 调用链/架构/数据流类问题，**主调用图已由系统自动展示在你回答的开头**——你的正文承接它做解释（可说"如上图"），**无需自己调画图工具画主图**，更绝不手画 mermaid/reactflow。
```

- [ ] **Step 3: 改 build_user_prompt free_format 分支**（prompts.py，free_format=True 的任务块，约 L456-457）

找到：
```python
        parts.append("用自然的 markdown 作答（标题/列表/代码块按需），不必套固定结构。")
        parts.append("提到方法/类/表时用 `[entity_id|显示文本]` 标注；只能基于 context/工具返回的真实实体，不得编造 entity_id。")
```
在其后插入：
```python
        # A2：架构类问题主调用图由系统自动展示在回答开头，agent 专注文字、不必再调画图工具
        parts.append("如本题适合调用图，**主图已由系统自动展示在你回答的开头**，正文承接解释即可（可说\"如上图\"），无需自己调画图工具画主图。")
```

Run: `venv/bin/python -m pytest tests/test_auth/test_diagram_render_tool_only.py -q` → PASS（含既有 + 2 新）

- [ ] **Step 4: Commit** — `git add -A && git commit -m "feat(qa): prompt 告知 agent 主调用图已自动展示、无需调画图工具画主图（A2）"`

---

## Task 4: 全量回归

- [ ] **Step 1:** `venv/bin/python -m pytest tests/test_auth/ -q -p no:cacheprovider`
Expected: 全绿（新增 10 例 + 既有 823 不回归；尤其 6 段路径 / 召回门控 / 既有 tool_call 透传 / prompt 不变量 不受影响——注入只在 sse_emitter 的 ReAct 流式前、6 段路径不经过）。
- [ ] **Step 2:**（若红）按失败定位修；若既有 prompt 测试断言了被替换的旧句，更新为新行为。
- [ ] **Step 3: Commit**（如有修正）

---

## Task 5（部署，需用户授权，不自动执行）

- [ ] git bundle `fe6a06e..release-0513` over SSH → 服务器 `/opt/knowledge-engineering` `git fetch + merge --ff-only` → `systemctl restart ke-api` → `/health`。**需用户显式授权。** 前端零改、无需 rsync。

## Task 6（eval gate，需用户授权，不自动执行）

- [ ] **50 道真实业务问题**（架构/流程/调用类为主，覆盖订单/支付/购物车/会员/营销/内容/互动 + 跨模块），agent 路径（KE_QA_USE_REACT=1）批跑 → 核验：① **门控命中题的出图率**（目标≈100%，对比改造前 ~2/5）；② 图准确（节点锚定真实方法、中英双语、可点）；③ 正文承接图、内容不丢；④ 非节点-边题不强出图（门控合理）。
- [ ] 据结果**调参**：出图率不足 → 查 call_edges 召回（top_k / 多跳深度）；误出图 → 收紧门控；位置/措辞 → 调 prompt。复用 `/tmp/_eval50.py` 框架 + Claude 读 `/tmp/mall-portal-src` 判准。

---

## 自检（spec 覆盖 / 占位 / 类型一致）

- spec §四.1 门控纯函数 → Task 1 ✓（3 门控例）
- spec §四.2 build_auto_call_graph_event（复用渲染核心 + fail-soft + at=0 + 双语）→ Task 1 ✓（3 构建例）
- spec §四.3 sse_emitter 注入 → Task 2 ✓（命中/不命中 2 例）
- spec §四.4 prompt 微调（AGENT_SYSTEM_PROMPT + build_user_prompt free_format，不改 6 段）→ Task 3 ✓
- spec §六 测试（门控/构建/注入/prompt 不变量/回归）→ Task 1/2/3/4 ✓
- spec §七 eval gate 50 题 → Task 6（需授权）✓
- 类型一致：`_should_auto_render(ctx)->bool`、`build_auto_call_graph_event(ctx)->dict|None`（payload 键 phase/id/name/render/at 全程一致）、注入用 `pending_tool_events.append(("tool_call", ev))`（与既有 `_on_tool_call` 同元组形态）✓
- 不动量：6 段路径（free_format=False / QASynthesizer）不经注入；`render_call_graph` 工具仍注册；前端零改（复用现有 render+at 路径）✓
