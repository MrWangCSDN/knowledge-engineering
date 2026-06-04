# 逻辑图中文化（A1 锚定式 LLM 业务流抽象）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把答案里的 `call_chain` 方法调用图升级为「LLM 重抽象的中文业务流程图」——节点是中文业务步骤、锚定真实方法（可点击跳源码）、用 2b 解读做语义、接地校验防幻觉，LLM 失败时回退现有确定性方法图。

**Architecture:** 主路径翻转为 LLM 生成（喂 2b 解读 + A1 指令），确定性方法图（Fix-2 的 `_ensure_call_chain_section`）降为兜底。retriever 富集调用链方法的 2b 中文解读到 `ctx.callchain_node_summaries`；build_user_prompt 渲染解读块 + A1 指令；synthesizer 新增 `_ground_call_chain_sections` 接地校验（节点 entityId 必须 ∈ 调用链真实方法集，虚构节点丢弃，有效<2 判废→兜底），接在 `_repair` 后、`_ensure` 前。无额外 LLM 调用、前端零改。

**Tech Stack:** Python 3.11 / FastAPI / pytest；CodeGraph(SQLite) + Weaviate(TopologicalInterpretation) + DashScope LLM；前端 React + ReactFlow（本计划不改）。

设计 spec：`/Users/java/obsidian/01 Engineering/knowledge-engineering/逻辑图中文化-设计.md`

---

## File Structure（改动边界）

| 文件 | 职责 | 改动 |
|---|---|---|
| `src/service/qa_engine/retriever.py` | 召回 + 上下文富集 | `RetrievedContext` 加 `callchain_node_summaries`；architecture 分支富集 2b 解读 |
| `src/service/qa_engine/prompts.py` | 拼 LLM user prompt | build_user_prompt 渲染「调用链方法业务解读」块 + 追加 A1 指令 |
| `src/service/qa_engine/synthesizer.py` | 解析/校验/注入 sections | 新增 `_recalled_ids` + `_ground_call_chain_sections`；`_ctx_to_dict` 透传 summaries；synthesize/stream 接线 |
| `tests/test_auth/test_callchain_grounding.py` | 接地校验单测（新建） | `_ground_call_chain_sections` + `_recalled_ids` |
| `tests/test_auth/test_qa_retriever.py` | retriever 单测（追加） | 富集 callchain_node_summaries |
| `tests/test_auth/test_qa_prompts.py` | prompt 单测（追加） | 解读块 + A1 指令渲染 |
| `tests/test_auth/test_callchain_inject.py` | 注入/兜底单测（追加） | _ctx_to_dict 透传 + 判废兜底回归 |

**不动**：composite_knowledge_store / 召回门控 / sse_emitter / qa_router / 前端（CallChain schema 已支持中文 label+edge.label+entityId、MethodNode 已渲染 kind/classOf/entityId）。

---

## Task 1: retriever 富集调用链方法的 2b 中文解读

**Files:**
- Modify: `src/service/qa_engine/retriever.py`（`RetrievedContext` dataclass ~L54-82；architecture 分支 for-loop 后 ~L186 `return ctx` 前）
- Test: `tests/test_auth/test_qa_retriever.py`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_auth/test_qa_retriever.py` 末尾：

```python
# ───────── callchain_node_summaries 富集（逻辑图中文化 §4.1）─────────


@pytest.mark.asyncio
async def test_retrieve_enriches_callchain_node_summaries():
    """architecture 分支：为调用链方法批量查 2b 解读，存入 ctx.callchain_node_summaries。"""
    bs = MagicMock()
    bs.search_method_hits_by_text.return_value = [
        {"entity_id": "C::register#(p)", "level": "method", "summary_text": "x", "score": 0.9},
    ]
    # get_by_entity：C::register 有解读，B::save 有解读，未知的返回 None
    def _get(eid, level=None):
        return {
            "C::register#(p)": {"summary_text": "会员注册入口，校验后落库"},
            "B::save#(m)": {"interpretation_text": "写入会员表"},
        }.get(eid)
    bs.get_by_entity.side_effect = _get
    g = MagicMock()
    # 调用边：C::register → B::save（这两个端点应被富集）
    g.successors.side_effect = lambda n: ["B::save#(m)"] if n == "C::register#(p)" else []
    g.predecessors.return_value = []
    g.module_of.return_value = None
    r = QARetriever(interpretation_store=bs, graph=g)
    ctx = await r.retrieve(question="注册流程", project_id="p", top_k=5)
    # 两个端点都富集到中文解读
    assert ctx.callchain_node_summaries.get("C::register#(p)") == "会员注册入口，校验后落库"
    assert ctx.callchain_node_summaries.get("B::save#(m)") == "写入会员表"


@pytest.mark.asyncio
async def test_retrieve_enrich_skips_missing_and_does_not_raise():
    """get_by_entity 返回 None 或抛异常的节点跳过，不阻断整体。"""
    bs = MagicMock()
    bs.search_method_hits_by_text.return_value = [
        {"entity_id": "C::register#(p)", "level": "method", "summary_text": "x", "score": 0.9},
    ]
    bs.get_by_entity.side_effect = Exception("weaviate down")  # 富集查询全炸
    g = MagicMock()
    g.successors.side_effect = lambda n: ["B::save#(m)"] if n == "C::register#(p)" else []
    g.predecessors.return_value = []
    g.module_of.return_value = None
    r = QARetriever(interpretation_store=bs, graph=g)
    ctx = await r.retrieve(question="注册流程", project_id="p", top_k=5)
    # 异常被吞，summaries 为空，但 retrieve 不崩、call_edges 仍在
    assert ctx.callchain_node_summaries == {}
    assert ctx.call_edges_by_entry  # 调用边照常
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_retriever.py::test_retrieve_enriches_callchain_node_summaries tests/test_auth/test_qa_retriever.py::test_retrieve_enrich_skips_missing_and_does_not_raise -v`
Expected: FAIL（`AttributeError: 'RetrievedContext' object has no attribute 'callchain_node_summaries'`）

- [ ] **Step 3: 加 `RetrievedContext` 字段**

在 `src/service/qa_engine/retriever.py` 的 `RetrievedContext` dataclass 里，`recall_score` 字段后追加：

```python
    recall_score: float = 0.0
    """召回门控：top1 相似度（meta/route 事件透传，便于前端显示匹配度 + 调阈值）。"""

    callchain_node_summaries: dict[str, str] = field(default_factory=dict)
    """{ entity_id: 2b中文业务解读(截断) }。逻辑图中文化（[[逻辑图中文化-设计]] §4.1）：
    为 call_edges_by_entry 涉及的真实方法富集 2b 解读，喂 LLM 写准确的中文业务标签。"""
```

- [ ] **Step 4: 加富集逻辑**

在 architecture 分支 `for c in candidates[: self.TOP_N_FOR_CHAIN_EXPANSION]:` 循环**结束后**、`return ctx` **之前**插入（即当前 L186 `)` 与 L187 `return ctx` 之间）：

```python
        # 逻辑图中文化（[[逻辑图中文化-设计]] §4.1）：为调用链涉及的真实方法批量查 2b 中文业务解读，
        # 喂给 LLM 写准确的中文业务标签（A1 锚定式）。职责：retriever 管召回+富集，synthesizer 不碰 store。
        # 端点集 = call_edges_by_entry 所有边的去重 from/to（= 调用链上全部真实方法）。
        recalled_ids: set[str] = set()
        # dict.values() 是各入口的边列表；每条边是 (from_id, to_id) 二元组
        for edges in ctx.call_edges_by_entry.values():
            for frm, to in edges:
                recalled_ids.add(frm)
                recalled_ids.add(to)
        # 逐个查 2b 解读：composite.get_by_entity(entity_id, level=None) 永不抛、取不到返 None。
        # 规模 ~10-25 节点/次，循环单查可接受（批量接口列为后续优化，YAGNI）。
        for mid in recalled_ids:
            try:
                # interpretation_store 是 composite（内部按 self._project_id 绑 tenant）
                rec = self.interpretation_store.get_by_entity(mid)
            except Exception:
                # best-effort 富集：单节点查询异常吞掉，不阻断整体召回
                rec = None
            if not rec:
                continue
            # 兼容多种返回字段名（解读库列 interpretation_text/context_summary；适配器映射 summary_text）
            text = (
                rec.get("summary_text")
                or rec.get("interpretation_text")
                or rec.get("context_summary")
                or ""
            ).strip()
            if text:
                # 截断控 token（设计 §9：每条 ≤120 字）
                ctx.callchain_node_summaries[mid] = text[:120]
        return ctx
```

> **实现期核实**：确认 retriever 持有的 `self.interpretation_store` 是 composite 且 `get_by_entity(entity_id, level=None)` 内部用请求级 `_project_id` 绑 tenant（qa_router 每请求构造）。若已知方法查出 None，多半是 composite 非请求级绑定——但富集是 best-effort，None→跳过→prompt 无解读块→LLM 退用方法名，**不破坏功能**（优雅降级）。

- [ ] **Step 5: 跑测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_retriever.py -v`
Expected: PASS（含两个新测 + 原有 _bfs_edges/architecture 测试）

- [ ] **Step 6: 提交**

```bash
git add src/service/qa_engine/retriever.py tests/test_auth/test_qa_retriever.py
git commit -m "feat(callchain): retriever 富集调用链方法的 2b 中文解读到 ctx"
```

---

## Task 2: prompt 喂解读 + A1 业务流指令

**Files:**
- Modify: `src/service/qa_engine/synthesizer.py`（`_ctx_to_dict` 透传 callchain_node_summaries）
- Modify: `src/service/qa_engine/prompts.py`（build_user_prompt 多跳调用链块 ~L354-363 后加解读块 + A1 指令）
- Test: `tests/test_auth/test_qa_prompts.py`、`tests/test_auth/test_callchain_inject.py`

- [ ] **Step 1: 写失败测试（prompt 渲染）**

追加到 `tests/test_auth/test_qa_prompts.py` 末尾（若无 import 先加 `from src.service.qa_engine.prompts import build_user_prompt`）：

```python
def test_build_user_prompt_renders_callchain_summaries_and_a1():
    """callchain_node_summaries 非空 → 渲染「调用链方法业务解读」块 + A1 业务流指令。"""
    ctx = {
        "skill_id": "architecture",
        "entry_candidates": [{"entity_id": "C::register#(p)", "summary_text": "s", "level": "method"}],
        "call_edges_by_entry": {"C::register#(p)": [("C::register#(p)", "B::save#(m)")]},
        "callchain_node_summaries": {
            "C::register#(p)": "会员注册入口，校验后落库",
            "B::save#(m)": "写入会员表",
        },
    }
    out = build_user_prompt("注册流程", ctx)
    # 解读块标题 + 至少一条解读 + entityId（method:// scheme）
    assert "调用链方法业务解读" in out
    assert "会员注册入口，校验后落库" in out
    assert "method://C::register#(p)" in out
    # A1 指令关键词
    assert "锚定" in out and "严禁虚构" in out


def test_build_user_prompt_no_summary_block_when_empty():
    """callchain_node_summaries 为空 → 不渲染解读块（不空标题）。"""
    ctx = {
        "skill_id": "architecture",
        "entry_candidates": [{"entity_id": "C::register#(p)", "summary_text": "s", "level": "method"}],
        "call_edges_by_entry": {"C::register#(p)": [("C::register#(p)", "B::save#(m)")]},
        "callchain_node_summaries": {},
    }
    out = build_user_prompt("注册流程", ctx)
    assert "调用链方法业务解读" not in out
```

追加到 `tests/test_auth/test_callchain_inject.py`（_ctx_to_dict 透传）— 复用其已 import 的 `_ctx_to_dict` / `RetrievedContext`：

```python
def test_ctx_to_dict_includes_callchain_node_summaries():
    """_ctx_to_dict 必须透传 callchain_node_summaries（否则 prompt 拿不到解读）。"""
    ctx = RetrievedContext(question="q", project_id="p")
    ctx.callchain_node_summaries = {"C::register#(p)": "会员注册入口"}
    d = _ctx_to_dict(ctx)
    assert d.get("callchain_node_summaries") == {"C::register#(p)": "会员注册入口"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_prompts.py::test_build_user_prompt_renders_callchain_summaries_and_a1 tests/test_auth/test_callchain_inject.py::test_ctx_to_dict_includes_callchain_node_summaries -v`
Expected: FAIL（prompt 无解读块文本 / `_ctx_to_dict` 无 callchain_node_summaries 键）

- [ ] **Step 3: `_ctx_to_dict` 透传 summaries**

在 `src/service/qa_engine/synthesizer.py` 的 `_ctx_to_dict` 里，`call_edges_by_entry` 那行后追加（与现有 getattr 兼容风格一致）：

```python
        # C2/Fix-2：多跳调用边——build_user_prompt 的「调用链路」块 + 确定性注入都需要它。
        "call_edges_by_entry": getattr(ctx, "call_edges_by_entry", {}),
        # 逻辑图中文化（[[逻辑图中文化-设计]] §4.2）：调用链方法的 2b 中文解读，喂 LLM 写业务标签
        "callchain_node_summaries": getattr(ctx, "callchain_node_summaries", {}),
```

- [ ] **Step 4: build_user_prompt 加解读块 + A1 指令**

在 `src/service/qa_engine/prompts.py` 的多跳调用链块（`call_edges = context.get("call_edges_by_entry")` 那段，当前结尾 `parts.append(f"      {frm}  →  {to}")` 行）**之后**插入：

```python
    # 2c. 逻辑图中文化（[[逻辑图中文化-设计]] §4.2）：调用链方法的 2b 中文业务解读 + A1 业务流指令。
    # 让 LLM 把上面的方法调用链「重抽象」成中文业务流程图——每个业务步骤节点锚定一个真实方法。
    node_summaries = context.get("callchain_node_summaries") or {}
    if node_summaries:
        parts.append("")
        parts.append("调用链方法业务解读（画业务流程图时只能用这里列出的方法，按 entityId 锚定）:")
        # 逐方法列出：entityId（method:// scheme，节点 entityId 照搬此值）| 方法 | 中文解读
        for mid, summary in node_summaries.items():
            parts.append(f"  - entityId: method://{mid} | 方法: {mid} | 业务解读: {summary}")
        # A1 业务流抽象指令（追加在解读块后，约束 call_chain 段产出方式）
        parts.append("")
        parts.append("【画 call_chain 业务流程图时（A1 锚定式）】")
        parts.append("  1. 把上述调用链重写成中文业务步骤流：可把连续的几个方法合并成一个业务步骤；")
        parts.append("  2. 每个节点必须锚定到上面列出的某一个真实方法，entityId 照搬其 method:// 值（点击可跳源码）；")
        parts.append("  3. label 用中文业务动作（≤12 字，如「生成订单」「校验短信验证码」「发新人优惠券」）；")
        parts.append("  4. edge.label 用中文衔接（如「校验通过后」「下单成功触发」）；")
        parts.append("  5. 只能用上面列出的方法，严禁虚构代码里没有的步骤/方法。")
```

- [ ] **Step 5: 跑测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_prompts.py tests/test_auth/test_callchain_inject.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add src/service/qa_engine/prompts.py src/service/qa_engine/synthesizer.py tests/test_auth/test_qa_prompts.py tests/test_auth/test_callchain_inject.py
git commit -m "feat(callchain): prompt 喂 2b 解读 + A1 业务流抽象指令"
```

---

## Task 3: 接地校验 `_ground_call_chain_sections`（防幻觉核心）

**Files:**
- Modify: `src/service/qa_engine/synthesizer.py`（新增 `_recalled_ids` + `_ground_call_chain_sections` 模块函数；synthesize / synthesize_stream 接线）
- Test: `tests/test_auth/test_callchain_grounding.py`（新建）

- [ ] **Step 1: 写失败测试（新建文件）**

创建 `tests/test_auth/test_callchain_grounding.py`：

```python
"""接地校验 _ground_call_chain_sections 单测（[[逻辑图中文化-设计]] §4.3）。

A1 锚定式：LLM 产的 call_chain 节点 entityId 必须 ∈ 调用链真实方法集；虚构节点丢弃、
连带丢引用它的边；有效节点 < 2 → 删整段（交兜底重注入）。边只校验引用合法保留 node id（允许逻辑边）。
"""
import json

from src.service.qa_engine.retriever import RetrievedContext
from src.service.qa_engine.synthesizer import (
    _ground_call_chain_sections,
    _recalled_ids,
)


def _cc_section(nodes, edges):
    """构造一个 call_chain section dict（content 为 CallChain JSON 字符串）。"""
    return {"type": "call_chain", "title": "业务流程",
            "content": json.dumps({"nodes": nodes, "edges": edges}, ensure_ascii=False)}


def test_recalled_ids_collects_edge_endpoints():
    """_recalled_ids = call_edges_by_entry 所有边的去重端点集。"""
    ctx = RetrievedContext(question="q", project_id="p")
    ctx.call_edges_by_entry = {
        "C::reg#(p)": [("C::reg#(p)", "S::reg#(p)"), ("S::reg#(p)", "M::save#(m)")],
    }
    assert _recalled_ids(ctx) == {"C::reg#(p)", "S::reg#(p)", "M::save#(m)"}


def test_ground_keeps_real_nodes_and_drops_hallucinated():
    """节点 entityId 在召回集→保留；不在（虚构）→丢节点 + 丢引用它的边。"""
    recalled = {"C::reg#(p)", "S::reg#(p)"}
    sec = _cc_section(
        nodes=[
            {"id": "n1", "label": "注册入口", "entityId": "method://C::reg#(p)"},
            {"id": "n2", "label": "注册业务", "entityId": "method://S::reg#(p)"},
            {"id": "n3", "label": "虚构的发短信", "entityId": "method://X::sendSms#()"},  # 幻觉
        ],
        edges=[
            {"from": "n1", "to": "n2", "label": "校验通过后"},
            {"from": "n2", "to": "n3", "label": "虚构边"},  # 引用 n3 → 应删
        ],
    )
    out = _ground_call_chain_sections([sec], recalled)
    data = json.loads(out[0]["content"])
    ids = {n["id"] for n in data["nodes"]}
    assert ids == {"n1", "n2"}                         # 虚构 n3 被丢
    assert all(e["to"] != "n3" for e in data["edges"])  # 引用 n3 的边被丢
    assert {(e["from"], e["to"]) for e in data["edges"]} == {("n1", "n2")}


def test_ground_drops_whole_section_when_valid_nodes_lt_2():
    """接地后有效节点 < 2 → 整段被删（LLM 整体跑偏，交兜底）。"""
    recalled = {"C::reg#(p)"}
    sec = _cc_section(
        nodes=[
            {"id": "n1", "label": "注册入口", "entityId": "method://C::reg#(p)"},
            {"id": "n2", "label": "虚构", "entityId": "method://X::hallucinate#()"},
        ],
        edges=[{"from": "n1", "to": "n2"}],
    )
    out = _ground_call_chain_sections([sec], recalled)
    assert all(s.get("type") != "call_chain" for s in out)  # call_chain 段被删


def test_ground_tolerates_entityid_without_scheme():
    """节点 entityId 不带 scheme（裸 qn）也能匹配召回集。"""
    recalled = {"C::reg#(p)", "S::reg#(p)"}
    sec = _cc_section(
        nodes=[
            {"id": "n1", "label": "入口", "entityId": "C::reg#(p)"},       # 裸 qn
            {"id": "n2", "label": "业务", "entityId": "method://S::reg#(p)"},
        ],
        edges=[{"from": "n1", "to": "n2"}],
    )
    out = _ground_call_chain_sections([sec], recalled)
    data = json.loads(out[0]["content"])
    assert {n["id"] for n in data["nodes"]} == {"n1", "n2"}


def test_ground_noop_on_non_callchain_sections():
    """非 call_chain 段原样返回。"""
    secs = [{"type": "overview", "content": "视角：x"}]
    assert _ground_call_chain_sections(secs, {"C::reg#(p)"}) == secs


def test_ground_invalid_json_section_dropped():
    """call_chain content 非法 JSON → 判废删段（交兜底）。"""
    secs = [{"type": "call_chain", "content": "{bad json"}]
    out = _ground_call_chain_sections(secs, {"C::reg#(p)"})
    assert all(s.get("type") != "call_chain" for s in out)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_auth/test_callchain_grounding.py -v`
Expected: FAIL（`ImportError: cannot import name '_ground_call_chain_sections'`）

- [ ] **Step 3: 实现 `_recalled_ids` + `_ground_call_chain_sections`**

在 `src/service/qa_engine/synthesizer.py` 中，紧挨 `_ensure_call_chain_section` 定义**之前**（同区块、复用已有的 `_cc_head` 思路与 `json` import）新增：

```python
def _recalled_ids(ctx) -> set[str]:
    """从 ctx.call_edges_by_entry 汇总调用链上全部真实方法 id（边的去重端点集）。

    用作 A1 接地校验的「合法锚点全集」——真实方法即使无 2b 解读也算合法锚点。
    """
    ids: set[str] = set()
    # getattr 兼容：ctx 可能是旧实例 / 测试桩
    for edges in getattr(ctx, "call_edges_by_entry", {}).values():
        for frm, to in edges:
            ids.add(frm)
            ids.add(to)
    return ids


def _ground_call_chain_sections(sections: list[dict], recalled_method_ids: set[str]) -> list[dict]:
    """A1 接地校验（[[逻辑图中文化-设计]] §4.3）：LLM 产的 call_chain 节点必须锚定真实方法。

    规则：
      - 节点 entityId（剥 method:// scheme）∈ recalled_method_ids → 保留；不在（虚构）→ 丢节点；
      - 丢节点后，引用被丢节点的边一并删（避免悬挂边）；
      - 有效节点 < 2 → 该 call_chain 段判废、整段删除（交 _ensure 兜底重注入方法图）；
      - content 非法 JSON → 同样判废删段；
      - 边只校验 from/to 都引用保留下来的 node id（**允许逻辑边，不要求是真实 call**——抽象本质）。
    非 call_chain 段原样返回。
    """
    out: list[dict] = []
    for sec in sections:
        # 非 call_chain 段：原样保留
        if sec.get("type") != "call_chain":
            out.append(sec)
            continue
        # 解析 content（CallChain JSON 字符串）；非法 → 判废（不加入 out）
        try:
            data = json.loads(sec.get("content") or "")
            nodes = data.get("nodes") or []
            edges = data.get("edges") or []
        except (ValueError, TypeError):
            continue  # 判废：不保留该段
        # 逐节点接地：entityId 剥 scheme（split('://',1)[-1] 对裸 qn 无副作用）后判是否真实
        kept_nodes = []
        kept_node_ids: set[str] = set()
        for n in nodes:
            anchor = (n.get("entityId") or "").split("://", 1)[-1]
            if anchor in recalled_method_ids:
                kept_nodes.append(n)
                kept_node_ids.add(n.get("id"))
        # 有效节点 < 2 → 判废删段
        if len(kept_nodes) < 2:
            continue
        # 边：from/to 都在保留节点里才留（允许逻辑边，只防悬挂）
        kept_edges = [
            e for e in edges
            if e.get("from") in kept_node_ids and e.get("to") in kept_node_ids
        ]
        # 重写 content（保留 title 等其它字段）
        new_sec = dict(sec)
        new_sec["content"] = json.dumps(
            {"nodes": kept_nodes, "edges": kept_edges}, ensure_ascii=False
        )
        out.append(new_sec)
    return out
```

- [ ] **Step 4: 跑单测确认通过**

Run: `./venv/bin/python -m pytest tests/test_auth/test_callchain_grounding.py -v`
Expected: PASS（6 例）

- [ ] **Step 5: 接线 synthesize（非流式）**

在 `src/service/qa_engine/synthesizer.py` 的 `synthesize` 方法里，找到（Fix-2 加的）这两行：

```python
        # Fix-2：LLM 没产出 call_chain 但召回到调用链 → 用 ctx 的多跳边确定性注入一段（必出 ReactFlow）
        sections = _ensure_call_chain_section(sections, ctx)
```

在它**之前**插入接地校验（即 `_repair` 后、`_ensure` 前）：

```python
        # 逻辑图中文化（[[逻辑图中文化-设计]] §4.3）：A1 接地校验——LLM 产的 call_chain 节点
        # 必须锚定召回到的真实方法；虚构节点丢弃、有效<2 判废删段（交下方 _ensure 兜底）。
        sections = _ground_call_chain_sections(sections, _recalled_ids(ctx))
        # Fix-2：LLM 没产出 call_chain 但召回到调用链 → 用 ctx 的多跳边确定性注入一段（必出 ReactFlow）
        sections = _ensure_call_chain_section(sections, ctx)
```

- [ ] **Step 6: 接线 synthesize_stream（流式）**

在 `synthesize_stream` 里，找到（Fix-2 加的）：

```python
        # Fix-2：同非流式路径——无 call_chain 段则用 ctx 多跳边确定性注入
        sections = _ensure_call_chain_section(sections, ctx)
```

在它**之前**插入：

```python
        # 逻辑图中文化 §4.3：A1 接地校验（同非流式路径），删幻觉节点/判废段
        sections = _ground_call_chain_sections(sections, _recalled_ids(ctx))
        # Fix-2：同非流式路径——无 call_chain 段则用 ctx 多跳边确定性注入
        sections = _ensure_call_chain_section(sections, ctx)
```

- [ ] **Step 7: 跑 synthesizer 全测确认通过 + 兜底回归**

Run: `./venv/bin/python -m pytest tests/test_auth/test_qa_synthesizer.py tests/test_auth/test_qa_react_synthesizer.py tests/test_auth/test_callchain_inject.py tests/test_auth/test_callchain_grounding.py -v`
Expected: PASS（接地校验删掉 LLM 废段后，`_ensure_call_chain_section` 仍注入确定性方法图——Fix-2 兜底不破）

- [ ] **Step 8: 提交**

```bash
git add src/service/qa_engine/synthesizer.py tests/test_auth/test_callchain_grounding.py
git commit -m "feat(callchain): A1 接地校验 _ground_call_chain_sections + synthesize/stream 接线"
```

---

## Task 4: 全量回归 + 部署 + E2E（需用户授权部署）

**Files:** 无（验证 + 部署）

- [ ] **Step 1: 全量回归**

Run: `./venv/bin/python -m pytest tests/test_auth tests/test_knowledge tests/test_integrations -q`
Expected: 全 PASS（基线 819 + 本次新增约 11 例）

- [ ] **Step 2: 提交计划完成标记（若有零散改动）**

```bash
git status   # 确认干净
```

- [ ] **Step 3: 部署（需用户显式授权）**

GitHub 服务器侧 443 可能不通 → git bundle 走 SSH：
```bash
cd /Users/java/knowledge-engineering-auth
git bundle create /tmp/cc_logic.bundle <上次部署HEAD>..release-0513
scp -P 26666 /tmp/cc_logic.bundle root@103.47.81.50:/tmp/cc_logic.bundle
ssh -p 26666 root@103.47.81.50 'cd /opt/knowledge-engineering && git fetch /tmp/cc_logic.bundle release-0513 && git merge --ff-only FETCH_HEAD && systemctl restart ke-api && sleep 6 && systemctl is-active ke-api && curl -s -m 10 http://127.0.0.1:8000/health'
```

- [ ] **Step 4: 服务器侧 E2E 探针（需授权）**

对「用流程图展示会员注册流程」「下单流程」跑生产真实 retriever+synthesizer，确认：
- call_chain 段节点 label 为中文业务动作、entityId 锚定真实方法（∈ 召回集）；
- 无虚构节点；LLM 跑偏时回退确定性方法图（仍有图）。

- [ ] **Step 5: 更新 Obsidian 完成标记**

在 `逻辑图中文化-设计.md` 顶部 frontmatter `状态:` 改为「已实施+部署」，正文追加实施小结 + E2E 结果；更新项目 `_overview.md` / `index.md` / `log.md`。

---

## Self-Review（计划 vs spec）

- **§4.1 retriever 富集** → Task 1 ✓（字段 + 端点集 + get_by_entity 多字段兜底 + 异常跳过）
- **§4.2 prompt 喂解读 + A1 指令** → Task 2 ✓（_ctx_to_dict 透传 + build_user_prompt 解读块 + A1 五条规则）
- **§4.3 接地校验** → Task 3 ✓（entityId 剥 scheme 判真实、丢幻觉节点+悬挂边、有效<2 判废、非法 JSON 判废、逻辑边保留）
- **§4.4 兜底** → Task 3 Step 5/6 接线顺序（_ground 在 _ensure 前）+ Step 7 兜底回归 ✓（不改 _ensure）
- **§4.5 触发范围** → 不做问法路由：_ground/_ensure 对所有 architecture sections 生效 ✓
- **§6 测试** → retriever 富集 / 接地校验 6 例 / prompt 渲染 / 兜底回归 / E2E ✓
- **类型一致**：`callchain_node_summaries: dict[str,str]`、`_recalled_ids(ctx)->set[str]`、`_ground_call_chain_sections(sections, recalled_method_ids:set[str])->list[dict]` 跨 Task 一致 ✓
- **recalled_method_ids 来源**：统一 = call_edges 端点集（Task 1 富集端点集 / Task 3 `_recalled_ids`），非 summaries key 集 ✓
- 占位符扫描：无 TBD/TODO；每步含完整代码 ✓
