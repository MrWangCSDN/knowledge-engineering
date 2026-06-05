# 业务问答 agent 化输出改造（后端）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `ReActSynthesizer` 成为真·自由 markdown 输出，并新增"渲染类工具" `render_call_graph`（只回 ack 给 LLM、图数据走前端内联），加自适应硬护栏，异常降级 6 段。

**Architecture:** 复用现有 ReAct loop + 8 个调查工具 + graph adapter + `_build_call_chain_section_from_edges`。新增渲染工具 + loop 对 `render` 结果特判 + 护栏 + SSE 透传 render + prompt 去 6 段。**不动召回/检索/门控**。开关复用 `KE_QA_USE_REACT`，不自动翻默认。

**Tech Stack:** Python 3.11 / FastAPI / pytest / asyncio。设计见 Obsidian [[业务问答-agent化输出改造-设计]]。

**约束**：Python 中文逐行注释；TDD；frequent commits；设计文档 Obsidian 不双写；部署走 git bundle（GitHub 443 不通），属部署步骤需用户授权、本计划不自动部署。

---

## 现状关键事实（实现前必读）

- `ReActSynthesizer`（`src/service/qa_engine/react_synthesizer.py`）：`synthesize`(L74-193) / `synthesize_stream`(L204-343) 两个 ReAct 循环；`_build_tool_usage_hint`(L365-414) 拼工具指引——**rule 4（L403）仍要求"直接输出 6 段式 JSON"，是本次要删的核心**；`_execute_tool_call`(L435-446) 执行工具、异常转 `{"error":...}`。
- 工具模式：`Tool(name, description, input_schema, handler)`（dataclass，`tools/base.py`），工厂 `build_ke_xxx_tool(graph: GraphProto) -> Tool` 闭包 graph。`graph.successors/predecessors(entity_id)` 同步遍历。
- 调用图构建：`synthesizer.py` **模块级**函数 `_build_call_chain_section_from_edges(call_edges_by_entry, max_nodes=_CALLCHAIN_MAX_NODES, node_summaries=None)`(L599-677) 返回 `{"type":"call_chain","title":"调用链路","content":<JSON 字符串>}`，content = `{"nodes":[{id,label,classOf,kind,entityId}],"edges":[{from,to}]}`；入参 `call_edges_by_entry={entry_id:[(from,to),...]}`；`node_summaries={entity_id:中文解读}` 用于中文 label。辅助 `_cc_label`(L528)、`_cc_head`(L522) 也是模块级、可直接 import。
- SSE：`sse_emitter.py` `_on_tool_call(phase, call, result)`(L177-203) 把 tool 事件压栈，主循环 flush `format_sse("tool_call", payload)`；`complete` 时 payload 现含 `result_preview`（截断 600 字）。

---

## File Structure

- **Create** `src/service/qa_engine/tools/render_call_graph.py` — 渲染类工具：BFS 收集 entity 周边调用边 → 复用 `_build_call_chain_section_from_edges` → 产出 `{render, summary}`。
- **Modify** 工具注册工厂（grep `build_ke_callees_tool` 找到注册处，通常 `qa_router.py` 或 `tools/__init__.py` 的 registry 构造）— 注册 `render_call_graph`。
- **Modify** `src/service/qa_engine/react_synthesizer.py` — ① 工具结果含 `render` 时只回 summary 给 LLM；② 护栏（max_iterations 8 + 总超时 + 单工具超时）；③ `_build_tool_usage_hint` 去 6 段、加渲染指引。
- **Modify** `src/service/qa_engine/prompts.py` — `AGENT_SYSTEM_PROMPT`(L175-255) 去 6 段残留、明确自由 markdown。
- **Modify** `src/service/qa_engine/sse_emitter.py` — `_on_tool_call` complete 时透传 `render`。
- **Test** `tests/test_auth/test_render_call_graph.py`、`tests/test_auth/test_react_synthesizer_render.py`、`tests/test_auth/test_react_guardrails.py`、`tests/test_auth/test_agent_prompt_invariants.py`、`tests/test_auth/test_sse_render_passthrough.py`。

---

## Task 1: `render_call_graph` 渲染工具

**Files:**
- Create: `src/service/qa_engine/tools/render_call_graph.py`
- Test: `tests/test_auth/test_render_call_graph.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_auth/test_render_call_graph.py
"""render_call_graph 工具：从 entity_id 遍历图、复用调用图构建、产出 {render, summary}。"""
import asyncio  # 跑 async handler
import json     # 校验 render.data 是合法结构
from src.service.qa_engine.tools.render_call_graph import build_render_call_graph_tool


class _FakeGraph:
    """假 GraphProto：用邻接表模拟 successors/predecessors。"""
    def __init__(self, succ):
        self._succ = succ                       # {node: [子节点...]}
    def successors(self, nid):
        return list(self._succ.get(nid, []))    # 下游
    def predecessors(self, nid):
        # 反查：谁的 successors 里含 nid
        return [k for k, vs in self._succ.items() if nid in vs]


def test_render_call_graph_down_builds_payload():
    # generateOrder → lockStock / hasStock；下游 2 跳
    g = _FakeGraph({
        "Svc::generateOrder#(P)": ["Svc::lockStock#()", "Svc::hasStock#()"],
        "Svc::lockStock#()": ["Mapper::updateStock#()"],
    })
    tool = build_render_call_graph_tool(g)
    out = asyncio.run(tool.handler({"entity_id": "Svc::generateOrder#(P)", "direction": "down", "depth": 2}))
    # 含 render 块 + 一句 summary
    assert out["render"]["kind"] == "call_graph"
    data = out["render"]["data"]
    assert len(data["nodes"]) >= 3                       # generateOrder/lockStock/hasStock/updateStock
    assert {"from", "to"}.issubset(data["edges"][0].keys())
    assert "调用图" in out["summary"] and "节点" in out["summary"]


def test_render_call_graph_no_edges_returns_none_render():
    g = _FakeGraph({})                                    # 无邻居
    tool = build_render_call_graph_tool(g)
    out = asyncio.run(tool.handler({"entity_id": "Svc::lonely#()"}))
    assert out["render"] is None                          # 无图不渲染
    assert "未找到" in out["summary"]


def test_render_call_graph_missing_entity_id():
    tool = build_render_call_graph_tool(_FakeGraph({}))
    out = asyncio.run(tool.handler({}))
    assert out["render"] is None
    assert out.get("error")                               # 给 LLM 错误信号


def test_render_call_graph_label_uses_summary_lookup():
    # summary_lookup 提供中文解读 → label 取中文短语
    g = _FakeGraph({"Svc::pay#()": ["Svc::notify#()"]})
    tool = build_render_call_graph_tool(g, summary_lookup=lambda nid: "发起支付宝支付 返回表单" if "pay" in nid else "")
    out = asyncio.run(tool.handler({"entity_id": "Svc::pay#()", "direction": "down", "depth": 1}))
    labels = [n["label"] for n in out["render"]["data"]["nodes"]]
    assert any("支付" in l for l in labels)               # 中文 label 生效
```

Run: `pytest tests/test_auth/test_render_call_graph.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 2: 实现**

```python
# src/service/qa_engine/tools/render_call_graph.py
"""render_call_graph 工具：渲染类工具（副作用型）。

与调查类工具（ke_callees 等）的区别：
  - 调查类：返回数据全部回灌 LLM 上下文
  - 渲染类：图数据只走前端内联渲染；回灌 LLM 的只有一句 summary（省 token、防模型用文字复述图）
        → ReActSynthesizer 看到结果含 "render" 字段时，只把 summary 写进 LLM tool message（见 react_synthesizer 改造）

复用 synthesizer 的确定性调用图构建（_build_call_chain_section_from_edges），产出与
前端 CallChainFlow / tryParseCallChain 同构的 {nodes, edges}。
"""
from __future__ import annotations

# deque：BFS 队列（同 ke_impact 的遍历范式）
from collections import deque
# json：把 _build_call_chain_section_from_edges 的 content(JSON 字符串)解析回 dict 装进 render
import json
from typing import Any, Callable, Optional

# GraphProto：注入式图后端协议（与 ke_callees/ke_impact 同源，零依赖主仓实现）
from src.service.qa_engine.retriever import GraphProto
from src.service.qa_engine.tools.base import Tool
# 复用 synthesizer 既有的确定性调用图构建 + 短名工具（模块级函数，可直接 import）
from src.service.qa_engine.synthesizer import _build_call_chain_section_from_edges, _cc_label

# 默认/上限：防超大图把渲染跑爆（与 ke_impact 同口径）
_DEFAULT_DEPTH = 2
_MAX_DEPTH = 4
_MAX_EDGES = 60

# input_schema：MCP 兼容；entity_id 必填，direction/depth 选填
_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entity_id": {
            "type": "string",
            "description": "起始实体 ID，形如 OmsPortalOrderServiceImpl::generateOrder#(OrderParam)",
        },
        "direction": {
            "type": "string",
            "description": "down=下游调用图（它调用了谁）；up=上游调用图（谁调用它）",
            "enum": ["down", "up"],
            "default": "down",
        },
        "depth": {
            "type": "integer",
            "description": "遍历跳数",
            "default": _DEFAULT_DEPTH,
            "minimum": 1,
            "maximum": _MAX_DEPTH,
        },
    },
    "required": ["entity_id"],
}


def build_render_call_graph_tool(
    graph: GraphProto,
    *,
    summary_lookup: Optional[Callable[[str], str]] = None,
) -> Tool:
    """构造绑定到指定 GraphProto 的 render_call_graph 工具。

    :param graph: 图后端（同 ke_callees/ke_impact）
    :param summary_lookup: 可选，entity_id → 2b 中文解读（用于中文 label）；
        None 时 label 回退方法短名（仍可用，只是非中文业务名）。
    """

    async def handler(input: dict[str, Any]) -> dict[str, Any]:
        # 校验必填：缺 entity_id → 返回错误信号（render=None，LLM 看到 error 不会以为渲染成功）
        entity_id = input.get("entity_id")
        if not entity_id:
            return {"render": None, "summary": "缺少 entity_id，无法渲染调用图", "error": "missing required field: entity_id"}

        # direction 兜底：非 'up' 一律 'down'
        direction = "up" if input.get("direction") == "up" else "down"
        # depth 容错（LLM 可能传 string）+ 夹 [1, _MAX_DEPTH]
        try:
            depth = int(input.get("depth", _DEFAULT_DEPTH))
        except (TypeError, ValueError):
            depth = _DEFAULT_DEPTH
        depth = max(1, min(depth, _MAX_DEPTH))

        # 选邻居函数：down=successors / up=predecessors
        neighbors = graph.successors if direction == "down" else graph.predecessors

        # BFS 收集"边"（与 ke_impact 收集节点不同，调用图要的是边）
        edges: list[tuple[str, str]] = []
        seen_edges: set[tuple[str, str]] = set()
        seen_nodes: set[str] = {entity_id}
        queue: deque[tuple[str, int]] = deque([(entity_id, 0)])
        try:
            while queue:
                node, d = queue.popleft()
                if d >= depth:
                    continue
                for nxt in neighbors(node):
                    # 统一成"调用方→被调用方"方向：down 时 node→nxt；up 时 nxt→node
                    frm, to = (node, nxt) if direction == "down" else (nxt, node)
                    if (frm, to) in seen_edges:
                        continue
                    seen_edges.add((frm, to))
                    edges.append((frm, to))
                    if nxt not in seen_nodes:
                        seen_nodes.add(nxt)
                        queue.append((nxt, d + 1))
                    if len(edges) >= _MAX_EDGES:
                        break
                if len(edges) >= _MAX_EDGES:
                    break
        except Exception as e:
            # 图后端挂了 → 错误信号，agent 改用文字描述、不崩
            return {"render": None, "summary": "图后端异常，未生成调用图", "error": f"graph backend error: {e}"}

        if not edges:
            return {"render": None, "summary": f"未找到 {_cc_label(entity_id)} 的调用关系"}

        # 取节点中文解读（可选）：summary_lookup 逐个查，拼成 _build_call_chain_section_from_edges 要的 node_summaries
        summaries: dict[str, str] = {}
        if summary_lookup is not None:
            for nid in seen_nodes:
                try:
                    s = summary_lookup(nid)
                except Exception:
                    s = ""
                if s:
                    summaries[nid] = s

        # 复用确定性构建：传 {entity_id: edges}（_build_call_chain_section_from_edges 的 call_edges_by_entry 形态）
        section = _build_call_chain_section_from_edges({entity_id: edges}, node_summaries=summaries)
        if not section:
            # 全是框架噪声（getter/Example/CRUD）被滤光 → 不渲染空图
            return {"render": None, "summary": f"{_cc_label(entity_id)} 的调用关系均为框架噪声，未生成图"}

        # section["content"] 是 JSON 字符串，解析回 dict 放进 render.data（前端 CallChainFlow 直接吃）
        data = json.loads(section["content"])
        n = len(data.get("nodes", []))
        flow = "下游" if direction == "down" else "上游"
        return {
            "render": {"kind": "call_graph", "data": data},
            # 只此一句回灌 LLM：模型知道"图已渲染"，自然衔接"见下方调用图"，不再用文字复述
            "summary": f"已渲染 {_cc_label(entity_id)} 的{flow}调用图（{n} 节点）",
        }

    return Tool(
        name="render_call_graph",
        description=(
            "渲染调用关系图（可视化）。当问题涉及'调用链路/流程/它调了谁/谁调它'时调用，"
            "在答案里内联生成一张可点击的调用图。direction=down 下游、up 上游。"
            "图直接展示给用户，你只需在文字里自然提及'见下方调用图'，不要用文字复述图里的节点。"
        ),
        input_schema=_SCHEMA,
        handler=handler,
    )
```

Run: `pytest tests/test_auth/test_render_call_graph.py -v` → PASS（4 例）

- [ ] **Step 3: Commit**

```bash
git add src/service/qa_engine/tools/render_call_graph.py tests/test_auth/test_render_call_graph.py
git commit -m "feat(qa): render_call_graph 渲染类工具（复用调用图构建，产出 render+summary）"
```

---

## Task 2: 注册 render_call_graph 到工具 registry

**Files:**
- Modify: 工具注册处（先 `grep -rn "build_ke_callees_tool\|build_ke_impact_tool" src/` 定位 ToolRegistry 构造点，通常 `src/service/qa_engine/qa_router.py` 或 `tools/__init__.py`）
- Test: `tests/test_auth/test_render_call_graph.py`（追加注册断言）

- [ ] **Step 1: 定位注册处**

Run: `grep -rn "build_ke_callees_tool\|build_ke_impact_tool\|ToolRegistry()" src/ | grep -v test`
找到把 `build_ke_*_tool(graph)` 逐个 `registry.register(...)` 的那段（设为 `<REGISTRY_SITE>`）。

- [ ] **Step 2: 写失败测试（registry 含 render_call_graph）**

```python
# 追加到 tests/test_auth/test_render_call_graph.py
def test_render_call_graph_registered_in_factory():
    """工具工厂应注册 render_call_graph（与 ke_callees 等并列）。"""
    # 按 <REGISTRY_SITE> 的实际工厂函数名导入（示例：build_default_tool_registry）
    from src.service.qa_engine.qa_router import build_default_tool_registry  # 改成实际路径/名字
    class _G:
        def successors(self, n): return []
        def predecessors(self, n): return []
    reg = build_default_tool_registry(graph=_G())   # 按实际签名传参
    names = [t.name for t in reg.list_tools()]
    assert "render_call_graph" in names
```

Run: `pytest tests/test_auth/test_render_call_graph.py::test_render_call_graph_registered_in_factory -v`
Expected: FAIL（未注册）

- [ ] **Step 3: 在 `<REGISTRY_SITE>` 注册**

在已有 `registry.register(build_ke_impact_tool(graph))` 之类后追加（summary_lookup 若注册处能拿到解读 store/composite 就接上，拿不到先传 None，label 回退方法短名——后续增强）：

```python
# 渲染类工具：调用图（图数据走前端内联，summary 回灌 LLM）
from src.service.qa_engine.tools.render_call_graph import build_render_call_graph_tool
registry.register(build_render_call_graph_tool(graph))   # summary_lookup 可后续接 composite.get_by_entity
```

Run: `pytest tests/test_auth/test_render_call_graph.py -v` → PASS

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "feat(qa): 注册 render_call_graph 到工具 registry"
```

---

## Task 3: ReActSynthesizer 对 render 结果特判（只回 summary 给 LLM）

**Files:**
- Modify: `src/service/qa_engine/react_synthesizer.py`（`synthesize` L173-178、`synthesize_stream` L326-330 两处 tool message append；新增 helper）
- Test: `tests/test_auth/test_react_synthesizer_render.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_auth/test_react_synthesizer_render.py
"""渲染类工具结果只把 summary 回灌 LLM；render.data 不进 LLM 上下文。"""
from src.service.qa_engine.react_synthesizer import _tool_message_content


def test_render_result_feeds_only_summary():
    out = {"render": {"kind": "call_graph", "data": {"nodes": [1, 2, 3], "edges": []}}, "summary": "已渲染 X（3 节点）"}
    content = _tool_message_content(out)
    assert "已渲染 X" in content          # summary 回灌
    assert "nodes" not in content          # 图数据不灌（省 token、防复述）


def test_non_render_result_feeds_full_json():
    out = {"entity_id": "A::m#()", "callees": ["B::n#()"]}
    content = _tool_message_content(out)
    assert "callees" in content            # 调查类工具结果照常全量回灌
```

Run: `pytest tests/test_auth/test_react_synthesizer_render.py -v`
Expected: FAIL（`_tool_message_content` 不存在）

- [ ] **Step 2: 实现 helper + 在两处 append 用它**

在 `react_synthesizer.py` 顶部（class 外）加 helper：

```python
def _tool_message_content(tool_output: dict[str, Any]) -> str:
    """把工具结果序列化成要回灌 LLM 的 tool message content。

    渲染类工具（结果含 'render' 字段）：只回 summary（一句话）。
    原因：render.data 是给前端内联渲染的大块图 JSON，灌回 LLM 既费 token，
    又会诱导模型用文字复述图。LLM 只需知道"图已渲染"即可自然衔接。
    其余（调查类）工具：照常全量 json.dumps 回灌。
    """
    # dict.get('render') 非空 → 渲染类，只回 summary（缺 summary 兜底一句）
    if isinstance(tool_output, dict) and tool_output.get("render") is not None:
        return json.dumps({"ok": True, "summary": tool_output.get("summary", "已渲染")}, ensure_ascii=False)
    return json.dumps(tool_output, ensure_ascii=False)
```

把 `synthesize`（L173-178）和 `synthesize_stream`（L326-330）里两处：

```python
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(tool_output, ensure_ascii=False),
                })
```

改为：

```python
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    # 渲染类工具只回 summary 给 LLM；图走前端（on_tool_call 仍带全量 render）
                    "content": _tool_message_content(tool_output),
                })
```

> 注意：`on_tool_call("complete", tc, tool_output)`（L169 / L323）**仍传完整 `tool_output`**（含 render），不动——SSE 据此把 render 推给前端。

Run: `pytest tests/test_auth/test_react_synthesizer_render.py -v` → PASS

- [ ] **Step 3: Commit**

```bash
git add src/service/qa_engine/react_synthesizer.py tests/test_auth/test_react_synthesizer_render.py
git commit -m "feat(qa): ReAct 渲染类工具只回 summary 给 LLM（图数据仅走前端）"
```

---

## Task 4: ReActSynthesizer 护栏（轮数 8 + 总超时 + 单工具超时）

**Files:**
- Modify: `src/service/qa_engine/react_synthesizer.py`（`__init__` L56-72、`_execute_tool_call` L435-446、两个循环 L120 / L249）
- Test: `tests/test_auth/test_react_guardrails.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_auth/test_react_guardrails.py
"""护栏：默认 max_iterations=8；单工具超时转 error 信号不抛。"""
import asyncio
from src.service.qa_engine.react_synthesizer import ReActSynthesizer
from src.service.qa_engine.tools.base import Tool, ToolRegistry


def test_default_max_iterations_is_8():
    reg = ToolRegistry()
    synth = ReActSynthesizer(llm_provider=object(), tool_registry=reg)
    assert synth.max_iterations == 8


def test_single_tool_timeout_returns_error_signal():
    # 注册一个永远 hang 的工具，单工具超时应返回 {"error":...} 而非卡死/抛异常
    async def _hang(_input):
        await asyncio.sleep(10)
        return {}
    reg = ToolRegistry()
    reg.register(Tool(name="ke_hang", description="hang", input_schema={"type": "object"}, handler=_hang))
    synth = ReActSynthesizer(llm_provider=object(), tool_registry=reg, tool_timeout_sec=0.05)

    class _TC:
        id = "1"; name = "ke_hang"; arguments = {}
    out = asyncio.run(synth._execute_tool_call(_TC()))
    assert "error" in out and "timeout" in out["error"].lower()
```

Run: `pytest tests/test_auth/test_react_guardrails.py -v`
Expected: FAIL（默认还是 12；无 tool_timeout_sec 参数）

- [ ] **Step 2: 实现**

`__init__`（L56-72）签名加超时参数：

```python
    def __init__(
        self,
        *,
        llm_provider: ToolCallingLLMProto,
        tool_registry: ToolRegistry,
        max_iterations: int = 8,          # 12→8：收紧安全阀（自适应下正常远不到）
        total_timeout_sec: float = 75.0,  # 每请求 wall-clock 总预算（超时停循环、用已生成内容收尾）
        tool_timeout_sec: float = 20.0,   # 单工具超时（图遍历/读文件防卡死）
    ) -> None:
        self.llm = llm_provider
        self.tool_registry = tool_registry
        self.max_iterations = max_iterations
        self.total_timeout_sec = total_timeout_sec
        self.tool_timeout_sec = tool_timeout_sec
```

`_execute_tool_call`（L435-446）包 `asyncio.wait_for`：

```python
    async def _execute_tool_call(self, tc: ToolCall) -> dict[str, Any]:
        """执行一次 ToolCall；异常/超时一律转 dict 错误信号，永不抛。"""
        try:
            # 单工具超时：图遍历/读文件等防卡死；超时 → error 信号，LLM 换工具/收敛
            return await asyncio.wait_for(
                self.tool_registry.call(tc.name, tc.arguments),
                timeout=self.tool_timeout_sec,
            )
        except asyncio.TimeoutError:
            return {"error": f"tool timeout after {self.tool_timeout_sec}s: {tc.name!r}"}
        except ToolNotFound:
            return {"error": f"tool not registered: {tc.name!r}"}
        except Exception as e:
            return {"error": f"tool execution failed: {e}"}
```

两个循环（`synthesize` L120 / `synthesize_stream` L249）加总超时 deadline。在 `for _iteration in range(self.max_iterations):` 之前加：

```python
        import time  # monotonic 单调时钟（不受系统时间调整影响），算总超时 deadline
        _deadline = time.monotonic() + self.total_timeout_sec
```

并在每个循环体最上方加：

```python
            # 总超时护栏：超预算就停循环，用已生成内容收尾（见循环后兜底）
            if time.monotonic() > _deadline:
                break
```

> 循环后已有兜底（L181-193 / L332-343：`last_response`/`last_raw_output` → `_parse_sections` 或"未完成"段），超时走同一兜底即可，无需新增。可把兜底文案"达到 N 轮上限"补一句"或超时"。

Run: `pytest tests/test_auth/test_react_guardrails.py -v` → PASS

- [ ] **Step 3: Commit**

```bash
git add src/service/qa_engine/react_synthesizer.py tests/test_auth/test_react_guardrails.py
git commit -m "feat(qa): ReAct 护栏 — max_iterations 8 + 总超时 75s + 单工具超时"
```

---

## Task 5: prompt 去 6 段、改自由 markdown + 渲染指引

**Files:**
- Modify: `src/service/qa_engine/react_synthesizer.py` `_build_tool_usage_hint`（L365-414，重点删 rule 4 的"6 段 JSON"）
- Modify: `src/service/qa_engine/prompts.py` `AGENT_SYSTEM_PROMPT`（L175-255，去任何 6 段/sections JSON 残留）
- Test: `tests/test_auth/test_agent_prompt_invariants.py`

- [ ] **Step 1: 写失败测试（源码不变量）**

```python
# tests/test_auth/test_agent_prompt_invariants.py
"""agent 自由输出不变量：prompt 不再要求 6 段 JSON；含渲染工具指引。"""
from pathlib import Path

_RS = Path("src/service/qa_engine/react_synthesizer.py").read_text(encoding="utf-8")
_PR = Path("src/service/qa_engine/prompts.py").read_text(encoding="utf-8")


def test_tool_hint_no_six_section_json():
    # 截取 _build_tool_usage_hint 函数体
    i = _RS.index("def _build_tool_usage_hint")
    j = _RS.index("def _tools_to_openai_schema", i)
    body = _RS[i:j]
    assert "6 段" not in body and "6段" not in body        # 不再要求 6 段
    assert "render_call_graph" in body                      # 含渲染工具指引


def test_agent_system_prompt_free_markdown():
    i = _PR.index("AGENT_SYSTEM_PROMPT")
    body = _PR[i:i + 4000]
    assert "6 段" not in body and "6段" not in body          # 去 6 段残留
    # 自由 markdown 取向（任一关键词命中即可）
    assert ("自由" in body) or ("markdown" in body.lower()) or ("自然" in body)
```

Run: `pytest tests/test_auth/test_agent_prompt_invariants.py -v`
Expected: FAIL（现含 6 段；无 render_call_graph）

- [ ] **Step 2: 改 `_build_tool_usage_hint`**

把 rule 4（L403-405）整段替换为自由输出 + 渲染指引：

```python
4. **能直接答就别再调工具**。tool_call 仅用于"看了 candidates 还差关键信息"。
   **用自由、自然的 markdown 作答**（不要输出固定模板/JSON）：答案的结构、长度随问题深浅自适应——
   简单问题简短直答，复杂问题再展开。引用代码实体时**照抄 candidates 里的 entity_id**（前端据此可点击跳源码）。
   检索不到的就**如实说明"未检索到 X"**，不要编。

5. **涉及调用关系/流程/"它调了谁、谁调它"时，调 render_call_graph(entity_id, direction)** 内联一张可点击调用图；
   图会直接展示给用户，你只需在文字里自然提及"见下方调用图"，**不要用文字逐节点复述图**。
```

（原 rule 5/6 顺延为 6/7，内容不变。）

- [ ] **Step 3: 改 `AGENT_SYSTEM_PROMPT`**

读 `prompts.py:175-255`，删除任何"输出 6 段 / sections / JSON 数组"的指令，改为：自由 markdown 作答、结构随问题自适应、引用用 entity_id、可调工具（含 render_call_graph）、检索不到如实说。保留既有的角色设定/语气。

Run: `pytest tests/test_auth/test_agent_prompt_invariants.py -v` → PASS

- [ ] **Step 4: Commit**

```bash
git add src/service/qa_engine/react_synthesizer.py src/service/qa_engine/prompts.py tests/test_auth/test_agent_prompt_invariants.py
git commit -m "feat(qa): agent prompt 去 6 段、改自由 markdown + render_call_graph 指引"
```

---

## Task 6: SSE 透传 render 字段

**Files:**
- Modify: `src/service/qa_engine/sse_emitter.py` `_on_tool_call`（L177-203）
- Test: `tests/test_auth/test_sse_render_passthrough.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_auth/test_sse_render_passthrough.py
"""_on_tool_call complete 阶段：结果含 render → tool_call payload 带 render（供前端内联渲染）。"""
# 说明：_on_tool_call 是 stream_qa_answer 内部闭包，不可直接 import；
# 沿用本仓库源码不变量手法（见 chat.test.ts 类比），断言 sse_emitter 源码在 complete 分支透传 render。
from pathlib import Path

_SRC = Path("src/service/qa_engine/sse_emitter.py").read_text(encoding="utf-8")


def test_on_tool_call_passes_render():
    i = _SRC.index("async def _on_tool_call")
    j = _SRC.index("pending_tool_events.append((\"tool_call\"", i)
    body = _SRC[i:j + 200]
    # complete 分支把 result 里的 render 透传进 payload
    assert 'payload["render"]' in body or "'render'" in body
    assert "render" in body
```

Run: `pytest tests/test_auth/test_sse_render_passthrough.py -v`
Expected: FAIL

- [ ] **Step 2: 实现**

`_on_tool_call`（L199-202 complete 分支）补 render 透传：

```python
        else:  # complete
            # 结果可能很大，截断 600 字以内（足够前端展示概要）
            result_text = json.dumps(result or {}, ensure_ascii=False)
            payload["result_preview"] = result_text[:600]
            # 渲染类工具：透传 render（图数据）给前端内联渲染（CallChainFlow）；
            # 调查类工具无 render 字段，不受影响
            if isinstance(result, dict) and result.get("render") is not None:
                payload["render"] = result["render"]
```

Run: `pytest tests/test_auth/test_sse_render_passthrough.py -v` → PASS

- [ ] **Step 3: 全量回归**

Run: `pytest tests/test_auth/ -q`
Expected: 全绿（新增 + 既有 ReAct/synthesizer/sse 测试不回归）

- [ ] **Step 4: Commit**

```bash
git add src/service/qa_engine/sse_emitter.py tests/test_auth/test_sse_render_passthrough.py
git commit -m "feat(qa): SSE tool_call 事件透传 render（前端内联调用图）"
```

---

## Task 7（部署 + eval gate，需用户授权，不自动执行）

- [ ] 部署后端到蓝队云：git bundle over SSH（GitHub 443 不通）+ 重启 ke-api + 健康验证。**需用户显式授权。**
- [ ] **eval gate**：临时 `KE_QA_USE_REACT=1` 跑 20 题源码金标准 + rerank 对比脚本，确认 (a) 锚定方法 recall 不回归 (b) 答案质量不降 (c) 轮数/延迟分布在预期内 → 出报告交用户。
- [ ] ⚠️ **翻默认是用户保留的独立决策**（MEMORY：KE_QA_USE_REACT 勿自动翻）。报告交付后由用户决定。

---

## 自检（spec 覆盖 / 占位 / 类型一致性）

- spec §5.2 渲染契约 → Task 1（工具）+ Task 3（只回 summary）+ Task 6（SSE 透传）✓
- spec §7 护栏 → Task 4 ✓；spec §6 prompt → Task 5 ✓；spec §4/§8 不翻默认 → Task 7 标注 ✓
- 类型一致：`render` payload 形态 `{kind, data:{nodes,edges}}` 在 Task1 产出、Task3 判定、Task6 透传一致 ✓；`_tool_message_content` 在 Task3 定义并被两处循环引用 ✓
- 前端消费 render（有序段 + CallChainFlow 内联）见姊妹计划 `2026-06-04-qa-agent-free-output-frontend.md`。

**已知取舍（spec §7 降级）**：spec 写"agent 异常/空/超时 → QASynthesizer 6 段兜底"。本计划 v1 用 **ReAct 既有 in-band 兜底**（超时/达上限/空 → `_parse_sections` 或"未完成（或超时）"段；自由文本失败 → 单段 overview）覆盖常见失败，**未实现"重跑 QASynthesizer 全量 6 段"**（需在 sse_emitter 捕获 ReAct 异常后再跑一遍 QASynthesizer，加延迟+复杂度，且 agent 整体失败属罕见）。若 eval/实测发现 in-band 兜底不足，再补一个"硬降级 QASynthesizer"任务。此为有意 YAGNI，非遗漏。
