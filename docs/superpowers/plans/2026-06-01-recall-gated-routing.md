# 召回门控路由 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans。Steps 用 checkbox（`- [ ]`）语法。

**Goal:** 把 QA 的"走知识引擎(KE) vs 闲聊(chit-chat)"判定从关键词路由改成"先向量召回、按 top1 相似度门控"——召回得好(top1≥τ)走 KE(architecture 1跳)，召回不到走闲聊。去掉全部关键词路由。

**Architecture:** 召回只做一次（retriever 内），其分数既用于门控、命中又直接喂 KE。`composite.search_method_hits_by_text` 透出相似度分数；`QARetriever.retrieve` 按 top1 门控；`sse_emitter` 不再调 `SkillRouter.route`；`SkillRouter` 退出主链路。

**Tech Stack:** Python · FastAPI · pytest（pytest-asyncio）。

**设计 spec（已审批）:** `/Users/java/obsidian/01 Engineering/knowledge-engineering/召回门控路由-设计.md`

**用户偏好:** Python 代码中文逐行注释；设计文档在 Obsidian 不双写仓库。

**探索已确认的事实（实现照此）:**
- `WeaviateVectorStore.search_by_text/by_vector` 已返回 `list[tuple[entity_id, score]]`，`score = 1 - cosine距离`（越大越像）。无需改。
- `composite_knowledge_store.py:_code_fallback`（约 L293-307）遍历 `for (eid, _score) in hits` 时**丢弃了 `_score`**，返回 dict 只有 `entity_id/summary_text/level`。
- `retriever.py:QARetriever.__init__`（L89）：`def __init__(self, *, interpretation_store, graph)`。`retrieve`（L93-178）签名 `async def retrieve(self, *, question, project_id, top_k=5, skill_id="architecture")`，内部按 skill_id 分支：chit-chat 早返回空(L115-120)、business 重排(L132-133)、dependency 2跳(L139-145)、其余 1 跳；尾部对 top-3 候选取 callees/callers/table(L148-176)。
- `RetrievedContext`（L48-67）dataclass 字段：question/project_id/entry_candidates/callees_by_entry/callers_by_entry/table_access_by_entry/skill_id。
- `sse_emitter.py`（L106-154）：先 `router.route(question)` 得 skill_id 写进 meta(L115-124)，emit meta(L130)，emit step searching(L133)，再 `retrieve(**retrieve_kwargs)`（含 skill_id，L138-147）。meta 在 retrieve 之前；调用方没传 router 时 meta 不带 skill 字段（前端容忍缺失）。
- 实测：真问题 top1∈[0.50,0.70]、闲聊/离题 top1≤0.41 → τ=0.45 居中。

---

## File Structure

| 文件 | 职责 | 动作 |
|---|---|---|
| `src/knowledge/composite_knowledge_store.py` | 召回归一化透出 score | Modify（`_code_fallback`）|
| `src/service/qa_engine/retriever.py` | RetrievedContext 加 recall_score；retrieve 改召回门控；删死分支 | Modify |
| `src/service/qa_engine/sse_emitter.py` | 不再调 router；retrieve 不传 skill_id；新增 route 事件带 recall_score | Modify |
| `src/service/qa_router.py` | build_retriever_for_project 注入 recall_threshold（env） | Modify |
| `tests/test_knowledge/test_composite_knowledge_store.py` | 断言 _code_fallback 透出 score | Modify |
| `tests/test_auth/test_retriever_recall_gate.py` | 召回门控单测 | Create |
| `tests/test_auth/test_sse_emitter_recall.py` | sse_emitter 不调 router + route 事件 | Create |

---

## Task 1：composite `_code_fallback` 透出相似度分数

**Files:** Modify `src/knowledge/composite_knowledge_store.py`；Test `tests/test_knowledge/test_composite_knowledge_store.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_knowledge/test_composite_knowledge_store.py` 末尾追加：
```python
def test_code_fallback_surfaces_score():
    """_code_fallback 归一化的 dict 必须带 score（= code_store 返回的相似度），供召回门控用。"""
    # 假 code_store：search_by_text 返回 [(entity_id, score)]
    class _FakeCodeStore:
        def search_by_text(self, text, top_k, tenant=None):
            return [("OmsOrderService::generateOrder#()", 0.66), ("X::y#()", 0.51)]

    # 假解读库：返回空 → 触发 CodeEntity 兜底
    class _EmptyInterp:
        def search_method_hits_by_text(self, *, text, project_id, limit=5):
            return []

    from src.knowledge.composite_knowledge_store import CompositeKnowledgeStore
    store = CompositeKnowledgeStore(
        interpretation_store=_EmptyInterp(), code_store=_FakeCodeStore(), project_id="mall-swarm",
    )
    hits = store.search_method_hits_by_text(text="下单", project_id="mall-swarm", limit=5)
    assert hits[0]["entity_id"] == "OmsOrderService::generateOrder#()"
    assert hits[0]["score"] == 0.66           # 透出真实分数
    assert hits[1]["score"] == 0.51
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && python -m pytest tests/test_knowledge/test_composite_knowledge_store.py::test_code_fallback_surfaces_score -v`
Expected: FAIL（`KeyError: 'score'` —— 当前 dict 无 score）

- [ ] **Step 3: 改 `_code_fallback`**

`src/knowledge/composite_knowledge_store.py` 的 `_code_fallback` 里那段（约 L293-306）：
```python
        for (eid, _score) in hits:
            if eid in seen:
                continue
            seen.add(eid)
            results.append({
                "entity_id": eid,
                "summary_text": "",       # 实事求是：CodeEntity 没业务解读
                "level": "code_entity",   # 标记兜底来源
            })
        return results
```
改为（带上 score）：
```python
        for (eid, _score) in hits:
            if eid in seen:
                continue
            seen.add(eid)
            results.append({
                "entity_id": eid,
                "summary_text": "",        # 实事求是：CodeEntity 没业务解读
                "level": "code_entity",    # 标记兜底来源
                "score": _score,           # 相似度(1-cos距离)，供召回门控判 top1（设计 [[召回门控路由-设计]] §4）
            })
        return results
```

- [ ] **Step 4: 运行确认通过 + 回归**

Run: `python -m pytest tests/test_knowledge/test_composite_knowledge_store.py -q`
Expected: PASS（新测过 + 既有 composite 测试不破；既有测试不查 score 字段，多一个 key 不影响）

- [ ] **Step 5: 提交**

```bash
git add src/knowledge/composite_knowledge_store.py tests/test_knowledge/test_composite_knowledge_store.py
git commit -m "feat(retrieval): surface similarity score in CodeEntity fallback hits (for recall gating)"
```

---

## Task 2：RetrievedContext 加 recall_score + QARetriever 召回门控

**Files:** Modify `src/service/qa_engine/retriever.py`；Test `tests/test_auth/test_retriever_recall_gate.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_auth/test_retriever_recall_gate.py
"""召回门控：top1≥τ 走 architecture(查图)，top1<τ 走 chit-chat(空 ctx、不查图)。设计 [[召回门控路由-设计]]。"""
import pytest
from src.service.qa_engine.retriever import QARetriever


class _Store:
    """假 interpretation_store：按构造时给的 hits 返回（带 score）。"""
    def __init__(self, hits):
        self._hits = hits
    def search_method_hits_by_text(self, *, text, project_id, limit=5):
        return self._hits


class _Graph:
    """假 graph：记录是否被调用，用来断言低召回时不查图。"""
    def __init__(self):
        self.called = False
    def successors(self, entity_id, rel_type=None):
        self.called = True
        return ["A::b#()"]
    def predecessors(self, entity_id, rel_type=None):
        self.called = True
        return []


@pytest.mark.asyncio
async def test_high_recall_goes_architecture_and_walks_graph():
    """top1=0.7 ≥ τ(0.45) → architecture，candidates 非空，图导航被调用。"""
    hits = [{"entity_id": "Oms::gen#()", "summary_text": "", "level": "code_entity", "score": 0.7}]
    g = _Graph()
    r = QARetriever(interpretation_store=_Store(hits), graph=g, recall_threshold=0.45)
    ctx = await r.retrieve(question="下单流程", project_id="mall-swarm")
    assert ctx.skill_id == "architecture"
    assert ctx.entry_candidates and ctx.entry_candidates[0]["entity_id"] == "Oms::gen#()"
    assert ctx.recall_score == 0.7
    assert g.called is True                       # 过线 → 查了图


@pytest.mark.asyncio
async def test_low_recall_goes_chit_chat_and_skips_graph():
    """top1=0.3 < τ → chit-chat，candidates 空，图导航不被调用。"""
    hits = [{"entity_id": "Oms::gen#()", "summary_text": "", "level": "code_entity", "score": 0.3}]
    g = _Graph()
    r = QARetriever(interpretation_store=_Store(hits), graph=g, recall_threshold=0.45)
    ctx = await r.retrieve(question="你好", project_id="mall-swarm")
    assert ctx.skill_id == "chit-chat"
    assert ctx.entry_candidates == []
    assert ctx.recall_score == 0.3
    assert g.called is False                      # 没过线 → 没查图


@pytest.mark.asyncio
async def test_threshold_boundary_inclusive():
    """top1 == τ 算过线（≥）。"""
    hits = [{"entity_id": "X::y#()", "summary_text": "", "level": "code_entity", "score": 0.5}]
    r = QARetriever(interpretation_store=_Store(hits), graph=_Graph(), recall_threshold=0.5)
    ctx = await r.retrieve(question="q", project_id="p")
    assert ctx.skill_id == "architecture"


@pytest.mark.asyncio
async def test_empty_hits_goes_chit_chat():
    """召回空 → chit-chat（top1 缺省 0.0 < τ）。"""
    r = QARetriever(interpretation_store=_Store([]), graph=_Graph(), recall_threshold=0.45)
    ctx = await r.retrieve(question="q", project_id="p")
    assert ctx.skill_id == "chit-chat"
    assert ctx.entry_candidates == []


@pytest.mark.asyncio
async def test_interp_hit_without_score_treated_as_pass():
    """解读库命中但无 score → 视为 1.0（强信号→过线 architecture）。设计 §7。"""
    hits = [{"entity_id": "I::j#()", "summary_text": "业务解读", "level": "method"}]  # 无 score
    r = QARetriever(interpretation_store=_Store(hits), graph=_Graph(), recall_threshold=0.45)
    ctx = await r.retrieve(question="q", project_id="p")
    assert ctx.skill_id == "architecture"
    assert ctx.recall_score == 1.0
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_auth/test_retriever_recall_gate.py -v`
Expected: FAIL（`QARetriever.__init__` 不接受 `recall_threshold`；RetrievedContext 无 `recall_score`；retrieve 仍按 skill_id 入参分支）

- [ ] **Step 3: RetrievedContext 加 recall_score 字段**

`src/service/qa_engine/retriever.py` 的 `RetrievedContext`（约 L66 `skill_id` 之后）加一行：
```python
    skill_id: str = "architecture"
    """v1.1 router 决策出来的 skill 名；synthesizer 据此往 user prompt 加视角偏置提示。"""

    recall_score: float = 0.0
    """召回门控：top1 相似度（meta/route 事件透传，便于前端显示匹配度 + 调阈值）。"""
```

- [ ] **Step 4: QARetriever.__init__ 加 recall_threshold**

`src/service/qa_engine/retriever.py:QARetriever.__init__`（L89）改为：
```python
    def __init__(self, *, interpretation_store: InterpretationStoreProto, graph: GraphProto,
                 recall_threshold: float = 0.45):
        # interpretation_store：复合检索源（解读库优先、空/异常兜底 CodeEntity）
        self.interpretation_store = interpretation_store
        # graph：CodeGraph 图导航适配器（GraphProto）
        self.graph = graph
        # 召回门控阈值：top1 相似度 ≥ 它才走 KE，否则闲聊（设计 [[召回门控路由-设计]] §3）
        self.recall_threshold = recall_threshold
```

- [ ] **Step 5: 重写 retrieve 为召回门控**

把 `retrieve`（L93-178 整段方法体）替换为：
```python
    async def retrieve(
        self,
        *,
        question: str,
        project_id: str,
        top_k: int = 5,
    ) -> RetrievedContext:
        """召回门控主入口（设计 [[召回门控路由-设计]] §4）。

        1. 语义召回（带相似度分数）
        2. top1 < recall_threshold → 判闲聊：返回空 ctx，不查图
        3. top1 ≥ recall_threshold → architecture：1 跳上下游 + 表访问(best-effort)
        """
        # 1. 语义召回候选实体（composite：解读库优先，空/异常兜底 CodeEntity；命中带 score）
        candidates = self.interpretation_store.search_method_hits_by_text(
            text=question, project_id=project_id, limit=top_k
        )

        # 2. 召回门控：取 top1 相似度
        # c.get("score", 1.0)：CodeEntity 兜底命中带真实分数；解读库命中若无 score 视为 1.0（强信号→通过，设计 §7）
        # max(..., default=0.0)：候选为空时 top1=0.0（必然 < τ → 闲聊）
        top1 = max((c.get("score", 1.0) for c in candidates), default=0.0)

        # top1 没过线 → 判为闲聊：返回空 ctx（不查图、不喂代码），synthesizer 走友好引导
        if top1 < self.recall_threshold:
            return RetrievedContext(
                question=question, project_id=project_id,
                skill_id="chit-chat", recall_score=top1,
            )

        # 3. 过线 → KE(architecture)：装好 ctx，对 top-N 候选取 1 跳上下游 + 表访问
        ctx = RetrievedContext(
            question=question, project_id=project_id,
            skill_id="architecture", recall_score=top1,
        )
        ctx.entry_candidates = candidates
        # 只对 top-N 候选取调用链（控成本）
        for c in candidates[: self.TOP_N_FOR_CHAIN_EXPANSION]:
            entity_id = c.get("entity_id")
            if not entity_id:
                continue
            # 向下（callees）/ 向上（callers）各 1 跳
            ctx.callees_by_entry[entity_id] = self._bfs_chain(
                entity_id, direction="down", max_depth=1, max_nodes=self.MAX_CALLEES
            )
            ctx.callers_by_entry[entity_id] = self._bfs_chain(
                entity_id, direction="up", max_depth=1, max_nodes=self.MAX_CALLERS
            )
            # 数据表访问（best-effort；CodeGraph 无 accesses_table 边时返 []）
            ctx.table_access_by_entry[entity_id] = self._extract_table_access(entity_id)
        return ctx
```

- [ ] **Step 6: 删除已成死代码的关键词 skill 辅助方法**

`grep -nE "_rerank_for_business|_extract_tables_from_text|DEPENDENCY_BFS_DEPTH" src/service/qa_engine/retriever.py`，删除 `_rerank_for_business`、`_extract_tables_from_text` 两个方法定义及 `DEPENDENCY_BFS_DEPTH` 类常量（新 retrieve 不再引用它们；保留 `_bfs_chain` / `_extract_table_access` / `_extract_tables_from_text` 若被别处引用则保留——先 grep 全仓确认无其它引用：`grep -rn "_extract_tables_from_text\|_rerank_for_business" src/ | grep -v retriever.py`，无引用才删）。

- [ ] **Step 7: 运行确认通过 + 回归**

Run: `python -m pytest tests/test_auth/test_retriever_recall_gate.py tests/ -k "retriever" -q`
Expected: 新测 PASS；既有 retriever 测试若断言旧 skill_id 入参行为会 FAIL → 记录待 Task 5 修。

- [ ] **Step 8: 提交**

```bash
git add src/service/qa_engine/retriever.py tests/test_auth/test_retriever_recall_gate.py
git commit -m "feat(qa): recall-gated retrieval (top1>=threshold -> KE, else chit-chat); drop keyword sub-skills"
```

---

## Task 3：sse_emitter 去 router、emit route 事件

**Files:** Modify `src/service/qa_engine/sse_emitter.py`；Test `tests/test_auth/test_sse_emitter_recall.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_auth/test_sse_emitter_recall.py
"""sse_emitter 不再调 router；retrieve 不传 skill_id；召回后 emit route 事件带 skill_id+recall_score。"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from src.service.qa_engine.retriever import RetrievedContext


def _events(sse_text_list):
    """把若干 SSE 文本块解析成 [(event_name, data_dict)]。"""
    out = []
    for chunk in sse_text_list:
        ev, data = None, None
        for line in chunk.splitlines():
            if line.startswith("event:"):
                ev = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = json.loads(line[len("data:"):].strip())
        if ev:
            out.append((ev, data))
    return out


@pytest.mark.asyncio
async def test_emitter_uses_recall_not_router(monkeypatch):
    from src.service.qa_engine import sse_emitter as mod

    # retriever.retrieve 返回 architecture ctx（recall_score=0.66）；断言不带 skill_id 调用
    ctx = RetrievedContext(question="下单", project_id="mall-swarm", skill_id="architecture", recall_score=0.66)
    retriever = MagicMock()
    retriever.retrieve = AsyncMock(return_value=ctx)
    # synthesizer 产出一个最简答案流（具体形态按现有 synthesize_stream 接口；这里 mock 成空异步生成器）
    synthesizer = MagicMock()
    async def _fake_stream(*a, **k):
        if False:
            yield ""
    synthesizer.synthesize_stream = _fake_stream

    # router 传入一个 spy，断言它 route 不被调用
    router = MagicMock()

    chunks = []
    async for c in mod.sse_answer_stream(   # ← 用真实入口函数名（Step 2 grep 确认）
        question="下单", project_id="mall-swarm", session_id="s1",
        retriever=retriever, synthesizer=synthesizer, router=router,
    ):
        chunks.append(c)

    router.route.assert_not_called()                      # 不再用关键词路由
    # retrieve 调用不带 skill_id
    _, kwargs = retriever.retrieve.call_args
    assert "skill_id" not in kwargs
    # 有 route 事件且带 recall_score
    evs = dict((e, d) for e, d in _events(chunks))
    assert "route" in evs and evs["route"]["recall_score"] == 0.66
    assert evs["route"]["skill_id"] == "architecture"
```

> 注：测试里的入口函数名/synthesize 接口以仓库实际为准——Step 2 先 grep 校正。

- [ ] **Step 2: 校正入口名 + 运行确认失败**

Run: `grep -nE "async def .*answer_stream|async def .*sse|def format_sse|synthesize_stream|def .*emit" src/service/qa_engine/sse_emitter.py | head`
据此把测试里的 `sse_answer_stream` 改成真实入口名；再运行：
`python -m pytest tests/test_auth/test_sse_emitter_recall.py -v`
Expected: FAIL（当前会调 router.route，且无 route 事件）

- [ ] **Step 3: 改 sse_emitter——去 router 块**

`src/service/qa_engine/sse_emitter.py` 把 L114-124 的 router 块：
```python
    # 决策出来后存起来，下面 retriever.retrieve 还要用 skill_id
    skill_id_for_retriever: str | None = None
    if router is not None:
        # `route` 永远不抛错 —— 兜底到 architecture
        decision = router.route(question)
        meta_payload["skill_id"] = decision.skill_id
        meta_payload["route_source"] = decision.source
        # matched_keywords 在 UI 上能展示『识别到关键词：调用 / 依赖』方便调优
        if decision.matched_keywords:
            meta_payload["matched_keywords"] = decision.matched_keywords
        # 后面 retriever.retrieve 调用要用
        skill_id_for_retriever = decision.skill_id
```
整段删除（router 参数保留在签名里向后兼容，但不再使用；meta 不再带 skill_id——前端已容忍缺失）。

- [ ] **Step 4: 改 sse_emitter——retrieve 不传 skill_id + emit route 事件**

把 L138-147 的 retrieve 调用：
```python
    retrieve_kwargs: dict[str, object] = {
        "question": question,
        "project_id": project_id,
        "top_k": 5,
    }
    if skill_id_for_retriever is not None:
        retrieve_kwargs["skill_id"] = skill_id_for_retriever

    try:
        ctx = await retriever.retrieve(**retrieve_kwargs)
    except Exception as e:
```
改为：
```python
    try:
        # 召回门控：不传 skill_id，retrieve 内部按 top1 相似度决定 KE/闲聊（设计 [[召回门控路由-设计]]）
        ctx = await retriever.retrieve(question=question, project_id=project_id, top_k=5)
    except Exception as e:
```
然后在 `ctx` 拿到后、进入合成之前（紧接 retrieve 的 try 块成功之后）emit 一个 route 事件，把召回决策告诉前端：
```python
    # 召回决策事件：skill_id(architecture/chit-chat) + recall_score(top1 相似度)
    # 前端可显示"匹配度"；旧前端忽略未知事件不受影响
    yield format_sse("route", {
        "skill_id": ctx.skill_id,
        "recall_score": round(getattr(ctx, "recall_score", 0.0), 4),
    })
```
（放在 `ctx = await retriever.retrieve(...)` 成功之后、`step: chain_extraction` 之前。）

- [ ] **Step 5: 运行确认通过 + 回归**

Run: `python -m pytest tests/test_auth/test_sse_emitter_recall.py tests/ -k "sse or emitter" -q`
Expected: 新测 PASS；既有 sse 测试若断言 meta 带 skill_id/调 router 会 FAIL → Task 5 修。

- [ ] **Step 6: 提交**

```bash
git add src/service/qa_engine/sse_emitter.py tests/test_auth/test_sse_emitter_recall.py
git commit -m "feat(qa): sse_emitter drops keyword router, emits route event with recall_score"
```

---

## Task 4：qa_router 注入 recall_threshold（env 可配）

**Files:** Modify `src/service/qa_router.py`；Test `tests/test_auth/test_qa_router_tools_injection.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_auth/test_qa_router_tools_injection.py` 追加（复用该文件已有的 _Req/_State/fake_db 风格；下面自带最小桩）：
```python
@pytest.mark.asyncio
async def test_build_retriever_injects_recall_threshold(monkeypatch, tmp_path):
    """build_retriever_for_project 把 env KE_QA_RECALL_THRESHOLD 注入 QARetriever.recall_threshold；缺省 0.45。"""
    from unittest.mock import AsyncMock, MagicMock
    from src.service.qa_router import build_retriever_for_project
    import src.service.qa_engine.adapters as _adapters

    # 造真实存在的 .codegraph.db → resolve_graph_adapter 给真 adapter（懒打开）
    cg = tmp_path / ".codegraph"; cg.mkdir(); (cg / "codegraph.db").write_bytes(b"")

    class _State:
        weaviate_interp_store = object()
        weaviate_code_store = None
    class _Req:
        app = type("A", (), {"state": _State()})()

    fake_project = MagicMock(); fake_project.repo_local_path = str(tmp_path)
    fake_db = AsyncMock(); fake_db.get = AsyncMock(return_value=fake_project)
    monkeypatch.setattr(_adapters, "WeaviateTopologicalAdapter", lambda store: MagicMock())

    monkeypatch.setenv("KE_QA_RECALL_THRESHOLD", "0.6")
    r = await build_retriever_for_project("mall-swarm", _Req(), fake_db)
    assert r.recall_threshold == 0.6

    monkeypatch.delenv("KE_QA_RECALL_THRESHOLD", raising=False)
    r2 = await build_retriever_for_project("mall-swarm", _Req(), fake_db)
    assert r2.recall_threshold == 0.45
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_auth/test_qa_router_tools_injection.py::test_build_retriever_injects_recall_threshold -v`
Expected: FAIL（QARetriever 构造未传 recall_threshold）

- [ ] **Step 3: 改 build_retriever_for_project**

`src/service/qa_router.py` 的 `build_retriever_for_project` 最后构造 QARetriever 处（约 L123 `return QARetriever(interpretation_store=composite_store, graph=graph_adapter)`）改为：
```python
    import os
    # 召回门控阈值：env 可配，缺省 0.45（设计 [[召回门控路由-设计]] §6）
    # float(...) 把环境变量字符串转浮点；try 兜底防坏值
    try:
        recall_threshold = float(os.getenv("KE_QA_RECALL_THRESHOLD", "0.45"))
    except ValueError:
        recall_threshold = 0.45
    return QARetriever(
        interpretation_store=composite_store,
        graph=graph_adapter,
        recall_threshold=recall_threshold,
    )
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_auth/test_qa_router_tools_injection.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/service/qa_router.py tests/test_auth/test_qa_router_tools_injection.py
git commit -m "feat(qa): inject recall_threshold (env KE_QA_RECALL_THRESHOLD, default 0.45) into retriever"
```

---

## Task 5：回归收尾——清理依赖关键词路由/skill_id 入参的旧测

**Files:** 视 Task 2/3 回归失败而定（可能含 `tests/test_auth/test_qa_router_classifier.py`、`test_qa_router_chitchat.py`、依赖 `retrieve(skill_id=...)` 的旧 retriever/sse 测试）

- [ ] **Step 1: 跑全量 test_auth 找出受影响测试**

Run: `python -m pytest tests/test_auth/ -q 2>&1 | tail -30`
列出 FAIL 项。

- [ ] **Step 2: 逐个判定 + 修**

判定原则：
- 断言"关键词→某 skill"或"未命中→chit-chat 兜底"的 SkillRouter 测试：SkillRouter 已退出主链路。**保留**（它仍是合法的纯函数测试，不删）——除非该测试断言的是 sse_emitter 调 router（那类改为断言 retrieve 决策）。
- 断言 `retrieve(skill_id="dependency"/"data-flow"/"business")` 走特定分支的测试：这些分支已删 → **删除或改写**为召回门控语义（top1 门控）。
- 断言 sse meta 带 skill_id / route_source / matched_keywords 的测试：改为断言 route 事件带 skill_id/recall_score（与 Task 3 一致）。

对每个 FAIL：要么改断言对齐新行为，要么删除已无意义的用例（在 commit message 说明删因）。

- [ ] **Step 3: 全量回归绿**

Run: `python -m pytest tests/test_auth/ -q 2>&1 | tail -5`
Expected: 全绿（0 failed）。

- [ ] **Step 4: 提交**

```bash
git add -A tests/
git commit -m "test(qa): align/cleanup tests for recall-gated routing (drop keyword-skill assertions)"
```

---

## Task 6：部署 + 服务器侧 E2E（⚠️ 需用户授权部署）

> 前置：用户授权部署到蓝队云（`git pull + systemctl restart ke-api`）。本任务**不自动执行部署**。

- [ ] **Step 1: 推送**

```bash
cd /Users/java/knowledge-engineering-auth && git push origin release-0513
```

- [ ] **Step 2:（用户授权后）服务器拉取 + 重启**

```bash
ssh -p 26666 root@103.47.81.50 'cd /opt/knowledge-engineering && git -c safe.directory=/opt/knowledge-engineering pull --ff-only origin release-0513 && systemctl restart ke-api && sleep 4 && systemctl is-active ke-api'
```

- [ ] **Step 3: 服务器侧 E2E（编程式，放仓库根跑避开 /tmp/inspect.py 遮蔽）**

把一段脚本 scp 到 `/opt/knowledge-engineering/_recall_e2e.py`：对每个问题用 `build` 出的 composite + resolve_graph_adapter + QARetriever(recall_threshold=0.45) 跑 `retrieve`，打印 `skill_id + recall_score`：
- `OMS订单流程是怎么实现的` → 期望 `skill_id=architecture`、recall_score≥0.45、candidates 非空
- `加入购物车是怎么实现的` → architecture
- `你好` → `skill_id=chit-chat`、recall_score<0.45、candidates 空
- `今天天气怎么样` → chit-chat
运行：`cd /opt/knowledge-engineering && ./venv/bin/python _recall_e2e.py`，跑完 `rm -f _recall_e2e.py`。
Expected: 真问题→architecture、闲聊离题→chit-chat。

- [ ] **Step 4: 前端实测**

前端选 mall-swarm，问 `OMS订单流程是怎么实现的` → 应给出基于 CodeGraph 的真实答案（不再"未接入代码库"）；问 `你好` → 友好闲聊。

- [ ] **Step 5: 回填 Obsidian §10**

在设计文档 `召回门控路由-设计.md` §10 实施完成标记回填：各 commit、E2E 结果、部署 commit。**不双写仓库。**

---

## Self-Review

**1. Spec 覆盖（spec §1-§9）：** 透出 score（Task 1）✅；retrieve 召回门控 + τ + 删死分支（Task 2）✅；sse_emitter 去 router + route 事件 + recall_score（Task 3）✅；qa_router 注入 τ（env）（Task 4）✅；SkillRouter 退出主链路（Task 3 sse 不再用它；类暂留）✅；降级（空召回→chit-chat：Task 2 test_empty_hits；解读无 score→1.0：Task 2 test_interp_hit_without_score）✅；测试策略（§8）逐条对应 Task 1-3 单测 + Task 6 E2E ✅。meta 时序：保留 meta 在前(不带 skill)、召回后 emit route 事件——已在 Task 3 说明（前端容忍 meta 缺 skill、忽略未知 route 事件）。

**2. 占位符扫描：** Task 3 测试入口函数名标注"Step 2 grep 校正"（真实待查，给了 grep）；Task 5 是回归清理（依赖前序 FAIL，给了判定原则而非占位）；其余均有完整 before/after 代码 + 命令 + 期望。无 TBD/TODO。

**3. 类型一致性：** `recall_threshold`（float，构造参数，默认 0.45）在 Task 2 定义、Task 4 注入，一致；`recall_score`（RetrievedContext 字段，默认 0.0）在 Task 2 定义、Task 3 读取（route 事件）、一致；`score`（hit dict key）Task 1 写入、Task 2 读取（`c.get("score", 1.0)`）一致；`retrieve(question, project_id, top_k)` 新签名（去 skill_id）Task 2 定义、Task 3 调用一致。
