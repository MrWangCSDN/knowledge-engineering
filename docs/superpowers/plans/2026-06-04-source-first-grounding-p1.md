# source-first grounding P1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 召回后确定性预读 top-3 候选的真实方法源码注入 ctx + prompt，治 agent/6段 自由展开代码细节时的臆造（eval 50 题 22% 实质错误）。

**Architecture:** composite 新增 `get_code_snippet`（fail-soft 取真实源码）；retriever 在候选定稿后预读 top-3 整方法源码写入新字段 `candidate_code_snippets`；`_ctx_to_dict` 带出；`build_user_prompt` 渲染「真实源码片段」+ 框架句（代码细节以源码为准、解读仅业务提示）。两条 synthesizer 路径共享。**不动召回门控/排序/前端/系统提示主体。**

**Tech Stack:** Python 3.11 / pytest。设计见 Obsidian [[业务问答-源码优先接地-P1设计]]。

**约束**：TDD、frequent commits、Python 中文逐行注释；设计文档 Obsidian 不双写；部署 git bundle + eval gate 属部署后步骤，需用户授权、本计划不自动执行。

---

## 现状关键事实（实现前必读）

- `CompositeKnowledgeStore`（composite_knowledge_store.py）：`__init__` L129-146 持 `_code_store: Optional[_CodeStoreLike]`；`_CodeStoreLike` proto（L97-110）**只声明 `search_by_text`**（无 `get_by_entity_id`）→ `get_code_snippet` 必须 `hasattr`/`getattr` 防御。真实 code_store 实例（与 ke_read_entity 同源）有 `get_by_entity_id(eid) -> {name, entity_type, code_snippet}`。
- `retriever.py`：`RetrievedContext`（L55-86，字段含 `entry_candidates`/`callchain_node_summaries`）；`retrieve()` L163 `ctx.entry_candidates = candidates` 定稿 → L166-178 module enrich → L180-197 top-N 调用链 → L199+ callchain_node_summaries enrich。`self.interpretation_store` 即 composite。预读 enrich 插在 **module enrich 之后（L178 后）**。
- `synthesizer.py` `_ctx_to_dict`（L778-787）：把 ctx 关键字段转 dict 给 build_user_prompt；需加 `candidate_code_snippets`。
- `prompts.py` `build_user_prompt`（L287+）：候选渲染循环 L325-338（每候选输出 `entity_id [level] (模块)` + `业务说明: summary`）；`TOP_CANDIDATES_FOR_PROMPT` 切片在 L324。

---

## File Structure

- **Modify** `src/knowledge/composite_knowledge_store.py` — 新增 `get_code_snippet`（fail-soft）。
- **Modify** `src/service/qa_engine/retriever.py` — `RetrievedContext` 加字段 + 模块级 `_truncate_snippet` + `QARetriever._enrich_candidate_snippets` + retrieve() 调用。
- **Modify** `src/service/qa_engine/synthesizer.py` — `_ctx_to_dict` 带 `candidate_code_snippets`。
- **Modify** `src/service/qa_engine/prompts.py` — `build_user_prompt` 渲染源码 + 框架句。
- **Test**: `tests/test_auth/test_composite_get_code_snippet.py`、`tests/test_auth/test_retriever_snippet_preread.py`、`tests/test_auth/test_build_user_prompt_snippet.py`。

---

## Task 1: composite.get_code_snippet（fail-soft 取真实源码）

**Files:** Modify `src/knowledge/composite_knowledge_store.py`；Test `tests/test_auth/test_composite_get_code_snippet.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_auth/test_composite_get_code_snippet.py
"""composite.get_code_snippet：fail-soft 取真实 code_snippet（source-first grounding P1）。"""
from src.knowledge.composite_knowledge_store import CompositeKnowledgeStore


def _mk(code_store):
    # interpretation_store 构造时不被调用，传占位 object 即可
    return CompositeKnowledgeStore(interpretation_store=object(), code_store=code_store, project_id="p")


def test_returns_snippet_when_code_store_has_it():
    class _CS:
        def search_by_text(self, *a, **k): return []
        def get_by_entity_id(self, eid): return {"name": "m", "code_snippet": "public void m(){...}"}
    assert _mk(_CS()).get_code_snippet("A::m#()") == "public void m(){...}"


def test_none_when_code_store_is_none():
    assert _mk(None).get_code_snippet("A::m#()") is None


def test_none_when_code_store_lacks_get_by_entity_id():
    # _CodeStoreLike proto 只声明 search_by_text；无 get_by_entity_id 的 store 不能崩
    class _CS:
        def search_by_text(self, *a, **k): return []
    assert _mk(_CS()).get_code_snippet("A::m#()") is None


def test_none_when_get_by_entity_id_raises_or_empty():
    class _Raise:
        def search_by_text(self, *a, **k): return []
        def get_by_entity_id(self, eid): raise RuntimeError("boom")
    class _Empty:
        def search_by_text(self, *a, **k): return []
        def get_by_entity_id(self, eid): return None
    assert _mk(_Raise()).get_code_snippet("A::m#()") is None     # 异常 fail-soft
    assert _mk(_Empty()).get_code_snippet("A::m#()") is None     # 查不到
```

Run: `venv/bin/python -m pytest tests/test_auth/test_composite_get_code_snippet.py -q`
Expected: FAIL（get_code_snippet 不存在）

- [ ] **Step 2: 实现**（加到 `CompositeKnowledgeStore`，紧跟 `search_method_hits_by_text` 之后）

```python
    # ─── source-first grounding P1（[[业务问答-源码优先接地-P1设计]]）────────────
    def get_code_snippet(self, entity_id: str) -> Optional[str]:
        """取实体真实源码片段，供 retriever 预读注入 prompt。

        fail-soft（永不抛）：_code_store 为 None / 无 get_by_entity_id（proto 只声明
        search_by_text，鸭子类型防御）/ 查不到 / 异常 → 一律返 None，caller 跳过该候选。
        """
        store = self._code_store                      # 兜底数据源（CodeEntity vector store），可能为 None
        if store is None:                             # 解读-only 部署：无 code_store → 无源码
            return None
        getter = getattr(store, "get_by_entity_id", None)  # proto 未声明此方法，运行时探测
        if not callable(getter):                      # store 实例不支持按 id 取 → 优雅缺省
            return None
        try:
            record = getter(entity_id)                # {name, entity_type, code_snippet} 或 None
        except Exception:                             # 后端异常 fail-soft
            return None
        if not record:                                # 查不到该实体
            return None
        snippet = record.get("code_snippet")          # 真实源码片段
        return snippet if snippet else None           # 空串/None 归一为 None
```

Run: `venv/bin/python -m pytest tests/test_auth/test_composite_get_code_snippet.py -q` → PASS（4 例）

- [ ] **Step 3: Commit** — `git add -A && git commit -m "feat(qa): composite.get_code_snippet 取真实源码（fail-soft，P1 source-first grounding）"`

---

## Task 2: retriever 预读 top-3 整方法源码注入 ctx

**Files:** Modify `src/service/qa_engine/retriever.py`、`src/service/qa_engine/synthesizer.py`（_ctx_to_dict）；Test `tests/test_auth/test_retriever_snippet_preread.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_auth/test_retriever_snippet_preread.py
"""retriever 源码预读：top-3、整方法全文、超大截断、None 跳过、_ctx_to_dict 带出。"""
from src.service.qa_engine.retriever import _truncate_snippet, RetrievedContext, QARetriever
from src.service.qa_engine.synthesizer import _ctx_to_dict


def test_truncate_keeps_short_method_full():
    s = "line1\nline2\nline3"
    assert _truncate_snippet(s) == s                      # 常规方法全文返回


def test_truncate_marks_oversized_method():
    s = "\n".join(f"l{i}" for i in range(400))            # 400 行 > 300 上限
    out = _truncate_snippet(s)
    assert "已截断" in out and "400 行" in out
    assert out.count("\n") <= 305                         # 截到上限附近


def test_enrich_only_top3_and_skips_none():
    # 4 候选；getter 对第 2 个返回 None（应跳过）、其余返回源码；top-3 才预读
    snippets = {"A::a#()": "code A", "B::b#()": None, "C::c#()": "code C", "D::d#()": "code D"}
    class _Comp:
        def get_code_snippet(self, eid): return snippets.get(eid)
    class _Graph:
        def module_of(self, eid): return None
    r = QARetriever(interpretation_store=_Comp(), graph=_Graph(), recall_threshold=0.45)
    ctx = RetrievedContext(question="q", project_id="p")
    ctx.entry_candidates = [{"entity_id": k} for k in ["A::a#()", "B::b#()", "C::c#()", "D::d#()"]]
    r._enrich_candidate_snippets(ctx)
    assert set(ctx.candidate_code_snippets) == {"A::a#()", "C::c#()"}   # top-3 内、跳过 None 的 B；D 是第4个不预读
    assert ctx.candidate_code_snippets["A::a#()"] == "code A"


def test_enrich_noop_when_store_lacks_method():
    class _Comp:  # 无 get_code_snippet
        pass
    class _Graph:
        def module_of(self, eid): return None
    r = QARetriever(interpretation_store=_Comp(), graph=_Graph(), recall_threshold=0.45)
    ctx = RetrievedContext(question="q", project_id="p")
    ctx.entry_candidates = [{"entity_id": "A::a#()"}]
    r._enrich_candidate_snippets(ctx)                     # 不崩
    assert ctx.candidate_code_snippets == {}


def test_ctx_to_dict_carries_snippets():
    ctx = RetrievedContext(question="q", project_id="p")
    ctx.candidate_code_snippets = {"A::a#()": "code A"}
    assert _ctx_to_dict(ctx)["candidate_code_snippets"] == {"A::a#()": "code A"}
```

Run: `venv/bin/python -m pytest tests/test_auth/test_retriever_snippet_preread.py -q`
Expected: FAIL（_truncate_snippet / _enrich_candidate_snippets / 字段 / dict 键 不存在）

- [ ] **Step 2: RetrievedContext 加字段**（retriever.py，紧跟 `callchain_node_summaries` 字段 L86 后）

```python
    candidate_code_snippets: dict[str, str] = field(default_factory=dict)
    """{ entity_id: 真实方法源码(整方法全文，仅病态超大截断) }。source-first grounding P1
    （[[业务问答-源码优先接地-P1设计]]）：召回后预读 top-3 候选真实源码注入，治代码细节臆造。"""
```

- [ ] **Step 3: 加模块级 `_truncate_snippet` + 常量**（retriever.py 顶部 helper 区，import 之后）

```python
# 源码预读上限：整方法全文，仅病态超大方法截断（1M 窗口下常规方法 token 可忽略）。设计 §D2
_SNIPPET_MAX_LINES = 300
_SNIPPET_MAX_CHARS = 8000


def _truncate_snippet(snippet: str, max_lines: int = _SNIPPET_MAX_LINES, max_chars: int = _SNIPPET_MAX_CHARS) -> str:
    """整方法全文返回；仅当超 max_lines 行或 max_chars 字才截断 + 标注（提示可 ke_read_entity 取全文）。"""
    lines = snippet.splitlines()                          # 按行切（splitlines 不保留行尾换行）
    if len(lines) <= max_lines and len(snippet) <= max_chars:
        return snippet                                    # 常规方法：原样全文
    kept = "\n".join(lines[:max_lines])[:max_chars]       # 先按行截、再按字符兜底
    return kept + f"\n…（已截断，原方法共 {len(lines)} 行，调 ke_read_entity 取全文）"
```

- [ ] **Step 4: 加 `_enrich_candidate_snippets` 方法 + retrieve() 调用**

`QARetriever` 加常量 + 方法：

```python
    # 源码预读只取 top-K 候选（锚定方法几乎总在 top-3；控成本）
    _TOP_K_FOR_SNIPPET = 3

    def _enrich_candidate_snippets(self, ctx: RetrievedContext) -> None:
        """给 top-3 候选预读真实方法源码，写入 ctx.candidate_code_snippets（source-first grounding P1）。

        best-effort：interpretation_store(composite) 无 get_code_snippet（旧实例）→ 整体跳过；
        单候选取不到/None/异常 → 跳过该候选；永不抛、不改候选顺序与门控。
        """
        getter = getattr(self.interpretation_store, "get_code_snippet", None)  # composite 提供；旧实例无则跳过
        if not callable(getter):
            return
        for c in ctx.entry_candidates[: self._TOP_K_FOR_SNIPPET]:  # 仅 top-3
            eid = c.get("entity_id")
            if not eid:
                continue
            try:
                snippet = getter(eid)                     # composite.get_code_snippet 已 fail-soft
            except Exception:
                snippet = None
            if not snippet:                               # None/空 → 跳过该候选（无源码退回解读）
                continue
            ctx.candidate_code_snippets[eid] = _truncate_snippet(snippet)
```

retrieve() 内 module enrich 循环（L169-178）**之后**插一行调用：

```python
        # source-first grounding（P1，[[业务问答-源码优先接地-P1设计]]）：预读 top-3 候选真实源码注入，
        # 让 synthesizer 答代码细节（SQL/字段/存储/状态码）时有一手料、不臆造。
        # 候选已在上方定稿，本步只 enrich、不影响门控/排序。
        self._enrich_candidate_snippets(ctx)
```

- [ ] **Step 5: _ctx_to_dict 带出**（synthesizer.py L778-787 的 return dict 内，`callchain_node_summaries` 行旁加）

```python
        # source-first grounding P1：候选真实源码片段，build_user_prompt 渲染给 LLM 作代码事实依据
        "candidate_code_snippets": getattr(ctx, "candidate_code_snippets", {}),
```

- [ ] **Step 6: 跑测试** — `venv/bin/python -m pytest tests/test_auth/test_retriever_snippet_preread.py -q` → PASS（5 例）

- [ ] **Step 7: Commit** — `git add -A && git commit -m "feat(qa): retriever 预读 top-3 候选真实源码注入 ctx（P1 source-first grounding）"`

---

## Task 3: build_user_prompt 渲染源码 + 框架句

**Files:** Modify `src/service/qa_engine/prompts.py`；Test `tests/test_auth/test_build_user_prompt_snippet.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_auth/test_build_user_prompt_snippet.py
"""build_user_prompt：有 candidate_code_snippets 时渲染源码 + 框架句；无则不渲染（不回归）。"""
from src.service.qa_engine.prompts import build_user_prompt

_BASE = {
    "entry_candidates": [{"entity_id": "A::m#()", "level": "method", "summary_text": "业务X"}],
    "skill_id": "architecture",
}


def test_renders_snippet_and_framing_when_present():
    ctx = {**_BASE, "candidate_code_snippets": {"A::m#()": "public void m(){ dao.insert(); }"}}
    p = build_user_prompt("q", ctx)
    assert "真实源码片段" in p
    assert "dao.insert()" in p                            # 源码内容渲染进 prompt
    assert "以源码为准" in p                               # 框架句出现


def test_no_snippet_block_when_absent():
    p = build_user_prompt("q", _BASE)                     # 无 candidate_code_snippets
    assert "真实源码片段" not in p
    assert "以源码为准" not in p                           # 不回归（无源码不加框架句）
```

Run: `venv/bin/python -m pytest tests/test_auth/test_build_user_prompt_snippet.py -q`
Expected: FAIL

- [ ] **Step 2: 实现**（prompts.py build_user_prompt 候选块）

(a) 取 snippets dict（在 `candidates = context.get("entry_candidates") or []` 之后、L319 `if candidates:` 之前加）：

```python
    code_snippets = context.get("candidate_code_snippets") or {}  # {entity_id: 真实源码}，P1 source-first grounding
```

(b) 框架句（在 `parts.append("候选入口方法（按相关度倒序）:")` 之后加；仅有源码时加）：

```python
        if code_snippets:
            parts.append(
                "  （注：以下候选凡附【真实源码片段】的，代码细节——SQL/表名/字段/存储技术/方法调用/状态码"
                "——一律以源码为准，2b 业务说明仅作业务提示、不可当代码事实；引用仍用 entity_id。）"
            )
```

(c) 渲染源码（在候选循环里 `parts.append(f"     业务说明: {summary}")` L338 之后加）：

```python
            # source-first grounding P1：该候选预读到真实源码 → 附上（代码细节以此为准）
            snippet = code_snippets.get(entity_id)
            if snippet:
                parts.append("     【真实源码片段】(代码细节以此为准):")
                parts.append("     ```")
                for line in snippet.splitlines():         # 逐行缩进对齐候选块
                    parts.append(f"     {line}")
                parts.append("     ```")
```

- [ ] **Step 3: 跑测试** — `venv/bin/python -m pytest tests/test_auth/test_build_user_prompt_snippet.py -q` → PASS（2 例）

- [ ] **Step 4: Commit** — `git add -A && git commit -m "feat(qa): build_user_prompt 渲染候选真实源码 + 框架句（P1 source-first grounding）"`

---

## Task 4: 全量回归

- [ ] **Step 1: 跑 test_auth 全量** — `venv/bin/python -m pytest tests/test_auth/ -q -p no:cacheprovider`
Expected: 全绿（新增 9 例 + 既有 798 不回归；尤其 retriever 门控/排序/recall_score 相关测试不受影响——enrich 在候选定稿后）。
- [ ] **Step 2:**（若有红）按失败定位修正；若是既有测试断言了"candidate 无 code_snippet 字段"之类，更新为新行为。
- [ ] **Step 3: Commit**（如有修正）

---

## Task 5（部署，需用户授权，不自动执行）

- [ ] git bundle（`<base>..release-0513`）over SSH → 服务器 `git fetch + merge --ff-only` → `systemctl restart ke-api` → `curl :8000/health`。**需用户显式授权。**

## Task 6（eval gate，需用户授权，不自动执行）

- [ ] 重跑 `/opt/knowledge-engineering/_eval50.py`（agent 自由输出，KE_QA_USE_REACT=1，已会自动经预读 enrich）→ 拉 JSON → 派 Claude 子代理读 `/tmp/mall-portal-src` 判准 → **对比改造前后**（基线见 [[mall-swarm-QA评测报告]] / 本轮 50 题：准确 17/部分 23/错误 11）。
- [ ] 成功标准：helper 臆造（会员 29）/状态机（订单 6）/流程类错误下降；准确+部分率上升、完全准确率上升、无新回归。Mapper SQL（内容域）/Mongo（互动域）类预期改善有限（属 B/C 档），据结果定是否上 B/C。

---

## 自检（spec 覆盖 / 占位 / 类型一致）

- spec §四.1 composite.get_code_snippet → Task 1 ✓（含 hasattr 防御 4 例）
- spec §四.2 RetrievedContext 字段 + 预读 helper（top-3/整方法/截断/best-effort）→ Task 2 ✓
- spec §四.3 build_user_prompt 渲染 + 框架句（不改系统提示主体）→ Task 3 ✓；_ctx_to_dict 带出 → Task 2 Step 5 ✓
- spec §六 测试（composite/retriever/prompt/不变量）→ Task 1/2/3 + Task 4 回归 ✓
- spec §七 eval gate → Task 6（需授权）✓
- 类型一致：`candidate_code_snippets: dict[str,str]` 在 RetrievedContext 定义、_enrich 写入、_ctx_to_dict 带出、build_user_prompt 读取（键名一致）；`get_code_snippet`/`_truncate_snippet`/`_enrich_candidate_snippets` 命名前后一致 ✓
- 不动召回门控/排序：enrich 在 `ctx.entry_candidates = candidates`（L164）+ rerank（L161）**之后**，只读候选 entity_id、不重排、不改 score ✓
