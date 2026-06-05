# reactflow 御用画图工具 + agent-native 画图 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans（本会话 inline 执行）。Steps 用 checkbox 跟踪。

**Goal:** 拆掉 A2 确定性注入，回归 agent-native 画图；把 `render_call_graph` 升级为通用 reactflow 工具（调用图 + 任意逻辑/架构图）；修"图跳到末尾 / reopen 丢图 / agent 手画 mermaid"三个渲染 bug。

**Architecture:** agent 在 ReAct 里自主调 `render_call_graph` → 图按 `at` 内联；后端把图折叠进 `sections`（持久化 + 有序）；prompt 强化"任何节点-边图唯一走该工具、严禁手画"；前端无条件剥手画图、删末尾补图。

**Tech Stack:** Python(FastAPI/pytest) 后端 `knowledge-engineering-auth`；React/TS(vitest) 前端 `knowledge-engineering-web`。设计 [[业务问答-reactflow御用画图工具-设计]]。

**约束:** TDD bite-sized；Python 中文逐行注释；frequent commits；设计文档 Obsidian 不双写；部署 git bundle / 前端 rsync 需用户授权、不自动；不动 6 段路径 / 召回门控 / P1 接地 / `CallChainFlow`·`MethodNode` 组件。

---

### Task 1: 拆掉 A2 确定性注入（sse_emitter + prompts + 测试）

**Files:**
- Modify: `src/service/qa_engine/sse_emitter.py`（删 `_should_auto_render` / `build_auto_call_graph_event` / 注入 L231-233）
- Modify: `src/service/qa_engine/prompts.py`（删 A2 行 L180、L430-431）
- Modify/Delete: `tests/test_auth/test_auto_callgraph_inject.py`；`tests/test_auth/test_diagram_render_tool_only.py`（删 `test_agent_prompt_says_main_graph_auto_shown` / `test_free_format_user_prompt_says_main_graph_auto_shown`）；`tests/test_auth/test_sse_emitter.py`（删 A2 注入断言）

- [ ] **Step 1: 改测试表达新不变量（红）** —— 在 `test_diagram_render_tool_only.py` 把两个 A2 断言换成反向：

```python
def test_agent_prompt_no_a2_auto_shown():
    """A2 撤销：AGENT_SYSTEM_PROMPT 不再声称'主图已自动展示'。"""
    assert "自动展示" not in AGENT_SYSTEM_PROMPT

def test_free_format_user_prompt_no_a2_auto_shown():
    """A2 撤销：free_format user prompt 不再声称'主图已自动展示'。"""
    p = build_user_prompt("q", {"entry_candidates": [], "skill_id": "architecture"}, free_format=True)
    assert "自动展示" not in p
```

- [ ] **Step 2: 跑测试确认红** — `pytest tests/test_auth/test_diagram_render_tool_only.py -x`（新断言 fail，旧 A2 断言仍在则一并报错）。
- [ ] **Step 3: 删 sse_emitter A2 代码** — 删 `build_auto_call_graph_event`、`_should_auto_render` 两个模块级函数（约 L80-113）；删 `stream_qa_answer` 内注入块（`_auto_cg = build_auto_call_graph_event(ctx) ...` L231-233）。保留 `_build_call_chain_section_from_edges` 在 synthesizer（工具仍用）。
- [ ] **Step 4: 删 prompts A2 行** — 删 prompts.py L180（"主调用图已由系统自动展示在你回答的开头…"）整条 bullet；删 L430-431（A2 注释 + "主图已由系统自动展示…无需自己调画图工具画主图" 那句 append）。
- [ ] **Step 5: 删 A2 专属测试文件** — `git rm tests/test_auth/test_auto_callgraph_inject.py`；删 `test_sse_emitter.py` 里引用 `build_auto_call_graph_event`/`_should_auto_render`/auto_cg 的用例。
- [ ] **Step 6: 跑相关测试转绿** — `pytest tests/test_auth/test_diagram_render_tool_only.py tests/test_auth/test_sse_emitter.py -x`。
- [ ] **Step 7: Commit** — `git add -A && git commit -m "revert(qa): 拆掉 A2 确定性调用图注入，回归 agent-native 画图"`

---

### Task 2: `render_call_graph` 模式 B（freeform nodes/edges）

**Files:**
- Modify: `src/service/qa_engine/tools/render_call_graph.py`
- Test: `tests/test_auth/test_render_call_graph_freeform.py`（新建）

- [ ] **Step 1: 写失败测试（红）**

```python
"""模式 B：agent 直接给 nodes/edges → 渲染任意 reactflow 图（逻辑/架构图），不经图后端 BFS。"""
import asyncio
from src.service.qa_engine.tools.render_call_graph import build_render_call_graph_tool

class _StubGraph:  # 模式 B 不该用到图后端
    def successors(self, n): raise AssertionError("freeform 不应触图后端")
    def predecessors(self, n): raise AssertionError("freeform 不应触图后端")

def _run(coro): return asyncio.get_event_loop().run_until_complete(coro)

def test_freeform_nodes_edges_renders():
    tool = build_render_call_graph_tool(_StubGraph())
    out = _run(tool.handler({
        "nodes": [{"id": "a", "label": "下单", "code": "OrderController.submit", "kind": "controller"},
                  {"id": "b", "label": "扣库存", "code": "StockService.deduct", "kind": "service"}],
        "edges": [{"source": "a", "target": "b", "label": "调用"}],
    }))
    assert out["render"]["kind"] == "call_graph"
    data = out["render"]["data"]
    assert len(data["nodes"]) == 2 and len(data["edges"]) == 1
    assert any(n.get("label") == "下单" for n in data["nodes"])

def test_freeform_empty_nodes_is_error():
    tool = build_render_call_graph_tool(_StubGraph())
    out = _run(tool.handler({"nodes": [], "edges": []}))
    assert out["render"] is None and "error" in out

def test_entity_and_nodes_both_missing_is_error():
    tool = build_render_call_graph_tool(_StubGraph())
    out = _run(tool.handler({}))
    assert out["render"] is None and "error" in out
```

- [ ] **Step 2: 跑测试确认红** — `pytest tests/test_auth/test_render_call_graph_freeform.py -x`（缺模式 B → fail）。
- [ ] **Step 3: 实现模式 B** — `render_call_graph.py`：
  - `_SCHEMA` 加 `nodes`(array of object) + `edges`(array of object)；`entity_id` 从 `required` 移除（改为 handler 内"二选一"校验）。
  - handler 开头：`nodes = input.get("nodes"); edges = input.get("edges")`；若 `nodes` 非空 → 走 freeform 分支：裁剪到 `_MAX_EDGES`/节点上限，归一化节点为 `{id,label,code,kind}`、边为 `{source,target,label}`，直接组装 `data={"nodes":..., "edges":...}`（与 `_build_call_chain_section_from_edges` 产出同构 —— 实现前先读该函数确认 node/edge 字段名 `label`/`method`/`kind`/`source`/`target`，freeform 字段名对齐它），返回 `{"render":{"kind":"call_graph","data":data}, "summary": f"已渲染逻辑图（{len(nodes)} 节点）"}`。
  - 若 `nodes` 空且无 `entity_id` → `{"render":None,"summary":"缺少 entity_id 或 nodes","error":"need entity_id or nodes"}`。
  - 有 `entity_id` 无 `nodes` → 走现有模式 A（不变）。
- [ ] **Step 4: 跑测试转绿** — `pytest tests/test_auth/test_render_call_graph_freeform.py -x`。
- [ ] **Step 5: Commit** — `git commit -am "feat(qa): render_call_graph 加 freeform 模式（agent 直接给 nodes/edges 画任意逻辑/架构图）"`

---

### Task 3: 后端 sections 按 `at` 折叠调用图（持久化 + 有序）

**Files:**
- Modify: `src/service/qa_engine/sse_emitter.py`（新增 `fold_render_sections` 纯函数 + 在 stream 收集 renders + on_complete 前折叠）
- Test: `tests/test_auth/test_fold_render_sections.py`（新建）

- [ ] **Step 1: 写失败测试（红）**

```python
"""把 agent 画的调用图按 at 偏移折叠进 sections：单 overview 文本 → [文本段, call_chain 段, 文本段]。"""
from src.service.qa_engine.sse_emitter import fold_render_sections

def test_fold_inserts_call_chain_by_at():
    sections = [{"type": "overview", "title": "回答", "content": "前文AAA中段BBB后文"}]
    renders = [{"at": 5, "data": {"nodes": [{"id": "x"}], "edges": []}}]  # at=5 → "前文AAA" 之后
    out = fold_render_sections(sections, renders)
    types = [s["type"] for s in out]
    assert "call_chain" in types
    cc = next(s for s in out if s["type"] == "call_chain")
    assert '"nodes"' in cc["content"]  # content 是 {nodes,edges} JSON 字符串（同 6 段 call_chain 约定）
    # call_chain 段在前文段之后、后文段之前
    assert types.index("call_chain") > 0 and types.index("call_chain") < len(types) - 0

def test_fold_no_renders_returns_sections_unchanged():
    sections = [{"type": "overview", "content": "无图"}]
    assert fold_render_sections(sections, []) == sections

def test_fold_at_zero_graph_first():
    sections = [{"type": "overview", "content": "正文"}]
    out = fold_render_sections(sections, [{"at": 0, "data": {"nodes": [], "edges": []}}])
    assert out[0]["type"] == "call_chain"  # at=0 → 图在最前
```

- [ ] **Step 2: 跑测试确认红** — `pytest tests/test_auth/test_fold_render_sections.py -x`。
- [ ] **Step 3: 实现 `fold_render_sections`** — sse_emitter 模块级纯函数：按 `at` 升序，在 overview 文本上切片（`text[:at]` / `text[at:]`），把每个 render 作为 `{"type":"call_chain","title":"调用图","content":json.dumps(data)}` 插入；空文本段丢弃；无 renders 原样返回；fail-soft（异常 → 返回原 sections）。**只折叠首个 overview/正文段**（agent 自由输出就是单段）。
- [ ] **Step 4: 跑测试转绿** — `pytest tests/test_auth/test_fold_render_sections.py -x`。
- [ ] **Step 5: 接线 stream_qa_answer** — 在 `_on_tool_call`（render 分支）收集 `rendered_graphs.append({"at": _offset[0], "data": result["render"]["data"]})`（仅 `result.render.kind=='call_graph'`）；流结束拿到 `answer` 后、`on_complete` 之前：`answer.sections = fold_render_sections(answer.sections, rendered_graphs)`（就地替换，使持久化 + cited 抽取都用折叠后 sections）。**先读 L355-445 确认 answer 变量名与 on_complete 调用点**。
- [ ] **Step 6: 接线测试（扩 test_sse_emitter）** — mock ReActSynthesizer 触发一次 render_call_graph（带 render），断言传给 on_complete 的 sections 含 `call_chain` 段。
- [ ] **Step 7: 跑 sse 测试转绿 + Commit** — `pytest tests/test_auth/test_sse_emitter.py tests/test_auth/test_fold_render_sections.py -x` → `git commit -am "fix(qa): agent 调用图按 at 折叠进 sections（持久化+有序，治跳末尾/reopen丢图）"`

---

### Task 4: prompt 强化 —— 唯一出口 + 双模式 + 先画后说 + 去 mermaid 教学

**Files:**
- Modify: `src/service/qa_engine/prompts.py`（AGENT_SYSTEM_PROMPT 画图段 L84-130 + L215-228；build_user_prompt free_format 画图指令 L383-395）
- Modify: `src/service/qa_engine/tools/render_call_graph.py`（Tool `description` 扩双模式）
- Test: `tests/test_auth/test_diagram_render_tool_only.py`（加不变量）

- [ ] **Step 1: 加不变量测试（红）**

```python
def test_agent_prompt_unifies_to_tool_only_no_mermaid_teaching():
    """唯一出口：AGENT_SYSTEM_PROMPT 不再教 mermaid 兜底画法；强调任何节点-边图都调工具。"""
    body = AGENT_SYSTEM_PROMPT
    assert "render_call_graph" in body
    assert "严禁" in body and "手画" in body
    # 不再把 mermaid graph 作为"兼容/兜底"画图方式教给 agent
    assert "兼容】Mermaid" not in body and "兜底；前端自动识别" not in body

def test_render_call_graph_desc_mentions_two_modes():
    from src.service.qa_engine.retriever import GraphProto  # noqa
    from src.service.qa_engine.tools.render_call_graph import build_render_call_graph_tool
    class _G:
        def successors(self,n): return []
        def predecessors(self,n): return []
    desc = build_render_call_graph_tool(_G()).description
    assert "nodes" in desc or "逻辑" in desc  # 描述提到 freeform/逻辑图能力
```

- [ ] **Step 2: 跑确认红** — `pytest tests/test_auth/test_diagram_render_tool_only.py -x`。
- [ ] **Step 3: 改 AGENT_SYSTEM_PROMPT** —（先读 L80-135、L210-230 精确文本）把 L84-130 的"【首选】JSON 调用图 / 【兼容】Mermaid 兜底"两段合并为**单一规则**：任何节点-边图（调用关系/业务流程/逻辑/架构/状态流转）一律 `render_call_graph`；代码调用→给 `entity_id`，业务逻辑/架构→给 `nodes/edges`；**严禁手画 mermaid graph|flowchart / reactflow**；时序图/ER/状态机才允许 mermaid。强化"先画图再展开解释"。
- [ ] **Step 4: 改 build_user_prompt free_format** — L383-395 画图指令同步成双模式（给真实 entityId 或 nodes/edges），去掉任何"6 段 call_chain"混淆（free_format 分支）。
- [ ] **Step 5: 改 tool description** — `render_call_graph.py` description 加："支持两种：① entity_id 出代码调用图；② nodes/edges 出任意逻辑/架构图。任何图都用我，别手画。"
- [ ] **Step 6: 跑转绿 + 6段不变量回归** — `pytest tests/test_auth/test_diagram_render_tool_only.py -x`（含 `test_six_section_user_prompt_unchanged`）。
- [ ] **Step 7: Commit** — `git commit -am "feat(qa): prompt 收敛为 render_call_graph 唯一画图出口（双模式+先画后说+去mermaid教学）"`

---

### Task 5: 前端 —— 删末尾补图 + 无条件剥手画图

**Files:**
- Modify: `knowledge-engineering-web/src/components/chat/AssistantMessage.tsx`（删 L524-530 末尾补图；L423 `stripReactflowFences` 去 `hasToolRender` 门槛）
- Test: `knowledge-engineering-web/src/components/chat/AssistantMessage.segments.test.tsx`（加源码断言）

- [ ] **Step 1: 加断言测试（红）**

```ts
it('完成态不再末尾一股脑补图（删除 tool_calls.render 末尾 map）', () => {
  // 源码不再包含"末尾补渲染"那段（按 render.kind==='call_graph' 在 sections 后 map）
  expect(src).not.toContain("filter((tc) => tc.render?.kind === 'call_graph')")
})
it('sections 文本无条件剥手画节点-边图（不再 gated on hasToolRender）', () => {
  expect(src).toContain('stripReactflowFences(s.content')
  expect(src).not.toContain('hasToolRender ? stripReactflowFences')
})
```

- [ ] **Step 2: 跑确认红** — `cd knowledge-engineering-web && npx vitest run AssistantMessage.segments`。
- [ ] **Step 3: 删末尾补图** — 删 `AssistantMessage.tsx:524-530`（`{Object.values(message.tool_calls...).filter(render.kind==='call_graph').map(CallChainFlow)}` 整块）。图现在由 sections 里的 call_chain 段渲染（Task 3 后端折叠保证）。
- [ ] **Step 4: 无条件剥手画图** — L423 `const body = hasToolRender ? stripReactflowFences(s.content || '') : (s.content || '')` → `const body = stripReactflowFences(s.content || '')`（永远剥节点-边手画图；sequence/ER 已被 stripReactflowFences 保留）。
- [ ] **Step 5: 跑转绿 + Commit** — `npx vitest run AssistantMessage.segments` → `git commit -am "fix(chat): 完成态按 sections 顺序渲染调用图（删末尾补图）+ 无条件剥手画节点-边图"`

---

### Task 6: 全量回归

- [ ] **Step 1: 后端** — `cd knowledge-engineering-auth && pytest tests/test_auth/ -q`（期望全绿；A2 测已删）。
- [ ] **Step 2: 前端** — `cd knowledge-engineering-web && npx vitest run`（期望全绿）。
- [ ] **Step 3: 修任何回归** — 失败逐个修，不放过。
- [ ] **Step 4: Commit（若有修）** — `git commit -am "test: 回归收尾"`

---

### Task 7: 部署 + eval（需用户授权，不自动执行）

- [ ] **Step 1: 后端部署** — git bundle `f848058..HEAD` → scp → server merge --ff-only → `systemctl restart ke-api` → `/health`。（**需授权**）
- [ ] **Step 2: 前端部署** — `npm run build` → rsync dist → `/opt/knowledge-engineering-web-dist`。（**需授权 + 确认**）
- [ ] **Step 3: eval gate** — 30 题真实业务问题 agent 路径跑：① 出图率（prompt 强化后实测基线）；② 图按 at 内联不跳、reopen 不丢；③ 正文无手画 mermaid 残留；④ 逻辑/架构类能用模式 B 出图。据结果调 prompt。（**需授权**）
- [ ] **Step 4: Obsidian 收尾** — 设计 spec 标"已实施"，A2 spec 标 superseded，_overview 登记新 spec + 移除 A2 状态。

---

## Self-Review
- **Spec 覆盖**：①拆A2=Task1；②freeform=Task2；③prompt=Task4+Task2(desc)；④渲染管线=Task3(后端折叠=①+②)+Task5(前端删末尾补图+无条件strip=③)。✅
- **占位扫描**：prompt/sse 精确文本在对应 Step 标注"先读 Lxx"——执行时读后改（inline 同会话执行，非零上下文交接）。
- **类型一致**：freeform 节点字段 `{id,label,code,kind}` 须对齐 `_build_call_chain_section_from_edges` 实际产出（Task2 Step3 已要求先读确认）；前端 render data 同构、组件不改。
