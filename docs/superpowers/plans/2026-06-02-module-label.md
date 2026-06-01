# 模块标签 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) 或 superpowers:executing-plans。Steps 用 checkbox（`- [ ]`）语法。

**Goal:** 给召回候选标注所属模块（mall-admin/mall-portal/...，来自 CodeGraph file_path 顶层目录），让 LLM 按 module 判前台/后台，修评测 Q5 误标（把 portal 当后台）。

**Architecture:** `CodeGraphGraphAdapter.module_of(entity_id)` 查 CodeGraph 得模块；`QARetriever.retrieve` 的 architecture 分支 best-effort 给候选注入 `module`；`build_user_prompt` 渲染 `(模块: x)` + 模块判断指引。仅标注、query-time、不重灌；composite/门控/sse/qa_router 不动。

**Tech Stack:** Python · pytest。

**设计 spec（已审批）:** `/Users/java/obsidian/01 Engineering/knowledge-engineering/模块标签-设计.md`

**用户偏好:** Python 中文逐行注释；设计文档 Obsidian 不双写。

**探索已确认的事实（实现照此）:**
- `CgNode`（db.py）有 `file_path` 字段（相对路径，如 `mall-portal/src/.../OmsPortalOrderController.java`）。模块 = `file_path.split('/', 1)[0]`。实测 `OmsOrderController`→`mall-admin`、`OmsPortalOrderController`→`mall-portal`。
- `CodeGraphGraphAdapter`（graph_adapter.py）已有 `_resolve(entity_id)->list[CgNode]`（split '#' 取 qn → `find_nodes_by_qualified_name`，重载多命中时按 durable_key 精筛）；已 import `sqlite3`、`Optional`、`_LOG`。
- `NullGraphAdapter`（graph_factory.py）现有 successors/predecessors→[]。
- `retriever.py`：`GraphProto` Protocol（L39-43，successors/predecessors）；`retrieve` architecture 分支设 `ctx.entry_candidates = candidates` 后对 top-N 取 callees/callers。
- `prompts.py:build_user_prompt`（L234-242）候选渲染：`for i, c in enumerate(candidates[:5], 1): entity_id=c.get("entity_id","?"); level=c.get("level","method"); summary=...; parts.append(f"  {i}. {entity_id}  [level={level}]"); parts.append(f"     业务说明: {summary}")`。

---

## File Structure

| 文件 | 职责 | 动作 |
|---|---|---|
| `src/integrations/codegraph/graph_adapter.py` | `CodeGraphGraphAdapter.module_of` | Modify |
| `src/integrations/codegraph/graph_factory.py` | `NullGraphAdapter.module_of`→None | Modify |
| `src/service/qa_engine/retriever.py` | `GraphProto.module_of` 声明 + retrieve 候选 enrich | Modify |
| `src/service/qa_engine/prompts.py` | build_user_prompt 渲染 module + 指引 | Modify |
| `tests/test_auth/test_module_of.py` | module_of / NullGraphAdapter 单测 | Create |
| `tests/test_auth/test_retriever_module_enrich.py` | retrieve 候选 enrich 单测 | Create |
| `tests/test_auth/test_prompt_module_label.py` | build_user_prompt 渲染单测 | Create |

---

## Task 1：module_of（CodeGraphGraphAdapter + NullGraphAdapter + Protocol）

**Files:** Modify `graph_adapter.py` / `graph_factory.py` / `retriever.py`(Protocol)；Create `tests/test_auth/test_module_of.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_auth/test_module_of.py
"""module_of：从 CodeGraph file_path 顶层目录取模块；查不到/异常→None。设计 [[模块标签-设计]]。"""
import sqlite3
from src.integrations.codegraph.graph_adapter import CodeGraphGraphAdapter
from src.integrations.codegraph.graph_factory import NullGraphAdapter


class _Node:
    """最小化假 CgNode：module_of 只读 file_path；_resolve 单命中不触发 durable_key。"""
    def __init__(self, file_path):
        self.id = "nid"; self.qualified_name = "X::y"; self.kind = "method"
        self.signature = "()"; self.file_path = file_path


class _FakeDB:
    def __init__(self, file_path=None, raise_err=False):
        self._fp = file_path; self._raise = raise_err
    def find_nodes_by_qualified_name(self, qn):
        if self._raise:
            raise sqlite3.OperationalError("db locked")
        return [_Node(self._fp)] if self._fp is not None else []


def test_module_of_portal():
    adp = CodeGraphGraphAdapter(_FakeDB("mall-portal/src/main/java/com/macro/mall/portal/controller/OmsPortalOrderController.java"))
    assert adp.module_of("OmsPortalOrderController::generateOrder#(OrderParamp)") == "mall-portal"


def test_module_of_admin():
    adp = CodeGraphGraphAdapter(_FakeDB("mall-admin/src/main/java/com/macro/mall/controller/OmsOrderController.java"))
    assert adp.module_of("OmsOrderController::list#()") == "mall-admin"


def test_module_of_unknown_returns_none():
    adp = CodeGraphGraphAdapter(_FakeDB(file_path=None))  # 无命中
    assert adp.module_of("Ghost::x#()") is None


def test_module_of_sqlite_error_returns_none():
    adp = CodeGraphGraphAdapter(_FakeDB(raise_err=True))
    assert adp.module_of("X::y#()") is None


def test_null_adapter_module_of_none():
    assert NullGraphAdapter().module_of("anything::x#()") is None
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python -m pytest tests/test_auth/test_module_of.py -v`
Expected: FAIL（`module_of` 不存在）

- [ ] **Step 3: CodeGraphGraphAdapter 加 module_of**

在 `src/integrations/codegraph/graph_adapter.py` 的 `CodeGraphGraphAdapter` 类里（`predecessors` 方法之后）加：
```python
    def module_of(self, entity_id: str) -> Optional[str]:
        """返回 entity 所属模块（CodeGraph file_path 顶层目录，如 'mall-portal'/'mall-admin'）。

        查不到节点 / sqlite 异常 → None（best-effort，同 _walk 的降级契约，绝不抛）。
        设计 [[模块标签-设计]] §3。
        """
        try:
            # 复用 _resolve：split '#' 取 qualified_name → find_nodes_by_qualified_name（重载多命中已处理）
            nodes = self._resolve(entity_id)
            if not nodes:                              # 无命中（qn 不在 CodeGraph）
                return None
            # 同一 qualified_name 的重载都在同一文件/同一模块，取首个即可
            fp = nodes[0].file_path or ""
            # 顶层目录即模块名：'mall-portal/src/...' → 'mall-portal'；无 '/' 时退化为整串或 None
            return fp.split("/", 1)[0] if "/" in fp else (fp or None)
        except sqlite3.Error as e:
            # 库缺失/锁/损坏 → 降级返回 None，不整体崩（对齐图导航降级风格）
            _LOG.warning("[codegraph] module_of 查询失败，返回 None (entity_id=%s): %s", entity_id, e)
            return None
```

- [ ] **Step 4: NullGraphAdapter 加 module_of**

在 `src/integrations/codegraph/graph_factory.py` 的 `NullGraphAdapter` 类里加（注意该文件已 import `Optional`）：
```python
    def module_of(self, entity_id: str) -> Optional[str]:
        """无 CodeGraph 索引 → 无模块信息（降级返 None，不报错）。"""
        return None
```

- [ ] **Step 5: GraphProto 加 module_of 声明（类型一致）**

在 `src/service/qa_engine/retriever.py` 的 `GraphProto`（L39-43）里 successors/predecessors 之后加一行声明：
```python
    def module_of(self, entity_id: str) -> str | None: ...
```

- [ ] **Step 6: 运行确认通过 + 回归**

Run: `python -m pytest tests/test_auth/test_module_of.py tests/ -k "graph_adapter or graph_factory or codegraph" -q`
Expected: 新测 PASS + 既有 CodeGraph 测试不破

- [ ] **Step 7: 提交**

```bash
git add src/integrations/codegraph/graph_adapter.py src/integrations/codegraph/graph_factory.py src/service/qa_engine/retriever.py tests/test_auth/test_module_of.py
git commit -m "feat(codegraph): module_of(entity_id) from file_path top dir (+ NullGraphAdapter, GraphProto)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2：retriever 候选 enrich module

**Files:** Modify `src/service/qa_engine/retriever.py`；Create `tests/test_auth/test_retriever_module_enrich.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_auth/test_retriever_module_enrich.py
"""retrieve 的 architecture 分支给候选注入 module（best-effort）；chit-chat 分支不调 module_of。"""
import pytest
from src.service.qa_engine.retriever import QARetriever


class _Store:
    def __init__(self, hits):
        self._hits = hits
    def search_method_hits_by_text(self, *, text, project_id, limit=5):
        return self._hits


class _Graph:
    def __init__(self):
        self.module_calls = []
    def successors(self, entity_id, rel_type=None):
        return []
    def predecessors(self, entity_id, rel_type=None):
        return []
    def module_of(self, entity_id):
        self.module_calls.append(entity_id)
        return "mall-portal" if "Portal" in entity_id else "mall-admin"


@pytest.mark.asyncio
async def test_architecture_enriches_module():
    hits = [{"entity_id": "OmsPortalOrderController::generateOrder#()", "summary_text": "", "level": "code_entity", "score": 0.7}]
    g = _Graph()
    r = QARetriever(interpretation_store=_Store(hits), graph=g, recall_threshold=0.45)
    ctx = await r.retrieve(question="下单", project_id="mall-swarm")
    assert ctx.skill_id == "architecture"
    assert ctx.entry_candidates[0]["module"] == "mall-portal"  # 注入了 module
    assert "OmsPortalOrderController::generateOrder#()" in g.module_calls


@pytest.mark.asyncio
async def test_chit_chat_does_not_call_module_of():
    hits = [{"entity_id": "X::y#()", "summary_text": "", "level": "code_entity", "score": 0.2}]  # 低召回→chit-chat
    g = _Graph()
    r = QARetriever(interpretation_store=_Store(hits), graph=g, recall_threshold=0.45)
    ctx = await r.retrieve(question="你好", project_id="mall-swarm")
    assert ctx.skill_id == "chit-chat"
    assert ctx.entry_candidates == []
    assert g.module_calls == []  # chit-chat 分支不 enrich


@pytest.mark.asyncio
async def test_module_of_failure_sets_none():
    class _BoomGraph(_Graph):
        def module_of(self, entity_id):
            raise RuntimeError("boom")
    hits = [{"entity_id": "X::y#()", "summary_text": "", "level": "code_entity", "score": 0.7}]
    r = QARetriever(interpretation_store=_Store(hits), graph=_BoomGraph(), recall_threshold=0.45)
    ctx = await r.retrieve(question="q", project_id="p")
    assert ctx.entry_candidates[0]["module"] is None  # 单个失败置 None，不崩
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_auth/test_retriever_module_enrich.py -v`
Expected: FAIL（候选无 "module" 键 / KeyError）

- [ ] **Step 3: retrieve architecture 分支加 enrich**

在 `src/service/qa_engine/retriever.py` 的 `retrieve` 里，architecture 分支设 `ctx.entry_candidates = candidates` 之后、对 top-N 取调用链的循环之前，插入：
```python
        # 给候选标注所属模块（best-effort）：让 LLM 按 module 判前台/后台，不凭名字臆断（设计 [[模块标签-设计]]）
        for c in ctx.entry_candidates:
            eid = c.get("entity_id")
            try:
                # graph.module_of：CodeGraph file_path 顶层目录（mall-portal/mall-admin/...）；查不到→None
                c["module"] = self.graph.module_of(eid) if eid else None
            except Exception:
                # 单个候选查模块失败不影响其余候选与主检索
                c["module"] = None
```

- [ ] **Step 4: 运行确认通过 + 回归**

Run: `python -m pytest tests/test_auth/test_retriever_module_enrich.py tests/test_auth/test_retriever_recall_gate.py -q`
Expected: PASS（新测 + 既有召回门控测试不破）

- [ ] **Step 5: 提交**

```bash
git add src/service/qa_engine/retriever.py tests/test_auth/test_retriever_module_enrich.py
git commit -m "feat(qa): enrich architecture candidates with module label (best-effort)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3：build_user_prompt 渲染 module + 指引

**Files:** Modify `src/service/qa_engine/prompts.py`；Create `tests/test_auth/test_prompt_module_label.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_auth/test_prompt_module_label.py
"""build_user_prompt：候选带 module 渲染出 (模块: x)；None 省略；含模块判断指引。"""
from src.service.qa_engine.prompts import build_user_prompt


def _ctx(cands):
    return {"entry_candidates": cands, "callees_by_entry": {}, "callers_by_entry": {},
            "table_access_by_entry": {}, "skill_id": "architecture"}


def test_renders_module_label():
    p = build_user_prompt("下单流程", _ctx([
        {"entity_id": "OmsPortalOrderController::generateOrder#()", "level": "code_entity",
         "summary_text": "", "module": "mall-portal"},
    ]))
    assert "(模块: mall-portal)" in p
    # 含模块判断指引（按 module 判前台/后台、别凭名字臆断）
    assert "mall-portal" in p and "mall-admin" in p and "臆断" in p


def test_omits_module_when_none():
    p = build_user_prompt("q", _ctx([
        {"entity_id": "X::y#()", "level": "code_entity", "summary_text": "", "module": None},
    ]))
    assert "(模块:" not in p          # module=None → 不渲染模块串
    # 全 None 时不应出现模块指引（避免无信息的噪声）
    assert "判断前台/后台" not in p


def test_no_module_key_safe():
    # 旧 ctx 候选没有 module 键也不报错（向后兼容）
    p = build_user_prompt("q", _ctx([{"entity_id": "X::y#()", "level": "code_entity", "summary_text": ""}]))
    assert "(模块:" not in p
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_auth/test_prompt_module_label.py -v`
Expected: FAIL（当前不渲染 module / 无指引）

- [ ] **Step 3: 改 build_user_prompt 候选渲染段**

`src/service/qa_engine/prompts.py` 把候选渲染循环（约 L234-242）：
```python
        for i, c in enumerate(candidates[:5], 1):
            entity_id = c.get("entity_id", "?")
            level = c.get("level", "method")
            summary = c.get("summary_text") or "(无业务说明)"
            # 截断过长的 summary 控制 token 数
            if len(summary) > 300:
                summary = summary[:300] + "…"
            parts.append(f"  {i}. {entity_id}  [level={level}]")
            parts.append(f"     业务说明: {summary}")
```
改为（带 module 渲染 + 末尾条件指引）：
```python
        for i, c in enumerate(candidates[:5], 1):
            entity_id = c.get("entity_id", "?")
            level = c.get("level", "method")
            module = c.get("module")  # 模块标签（mall-portal/mall-admin/...）；None/缺失则省略
            summary = c.get("summary_text") or "(无业务说明)"
            # 截断过长的 summary 控制 token 数
            if len(summary) > 300:
                summary = summary[:300] + "…"
            # 有 module 才拼 (模块: x)，避免 None 渲染成噪声
            mod_str = f"  (模块: {module})" if module else ""
            parts.append(f"  {i}. {entity_id}  [level={level}]{mod_str}")
            parts.append(f"     业务说明: {summary}")
        # 模块判断指引：仅当至少一个候选带 module 时追加，让 LLM 按 module 判前台/后台
        if any(c.get("module") for c in candidates[:5]):
            parts.append(
                "  （模块说明：mall-portal=前台门户、mall-admin=后台管理、mall-search=搜索、"
                "mall-auth=认证、mall-gateway=网关。判断前台/后台等归属请按上面标注的【模块】，"
                "不要仅凭类名/方法名臆断；若要对比的另一侧模块不在候选里，"
                "如实说\"未检索到 X 模块的相关实体\"。）"
            )
```
（注：测试断言 "判断前台/后台" 子串——指引里含"判断前台/后台等归属"，覆盖；module=None 时该 if 不触发、无指引。）

- [ ] **Step 4: 运行确认通过 + 回归**

Run: `python -m pytest tests/test_auth/test_prompt_module_label.py tests/ -k "prompt" -q`
Expected: PASS（新测 + 既有 prompt 测试不破）

- [ ] **Step 5: 全量回归**

Run: `python -m pytest tests/test_auth/ -q 2>&1 | tail -5`
Expected: 0 failed

- [ ] **Step 6: 提交**

```bash
git add src/service/qa_engine/prompts.py tests/test_auth/test_prompt_module_label.py
git commit -m "feat(qa): render candidate module label + module-judgment hint in user prompt

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4：部署 + 服务器侧 E2E（⚠️ 需用户授权部署）

> 前置：用户授权部署到蓝队云。本任务不自动部署。

- [ ] **Step 1: 推送** `cd /Users/java/knowledge-engineering-auth && git push origin release-0513`
- [ ] **Step 2:（授权后）服务器拉取 + 重启**
  `ssh -p 26666 root@103.47.81.50 'cd /opt/knowledge-engineering && git -c safe.directory=/opt/knowledge-engineering pull --ff-only origin release-0513 && systemctl restart ke-api && sleep 4 && systemctl is-active ke-api'`（github HTTPS 偶发 TLS 抖动 → 失败重试几次）
- [ ] **Step 3: 服务器侧 E2E**（放仓库根跑避开 /tmp/inspect.py 遮蔽）：跑 Q5「后台订单管理能做哪些操作？和前台用户下单有什么区别？」+ LLM 合成；
  - 期望：候选标注出 `module=mall-portal`；答案**正确说这些是 mall-portal（前台）订单操作、未检索到 mall-admin（后台）订单管理**（不再把 portal 当后台）。
  - 回归：Q1「下单流程」/ Q4「应付金额」答案不退化。
- [ ] **Step 4: 回填 Obsidian §9** —— `模块标签-设计.md` §9 记 commit、E2E（Q5 是否不再误标）、部署 commit。不双写仓库。

---

## Self-Review

**1. Spec 覆盖（§3-§8）：** 模块来源 file_path 顶层目录（§3）→ Task1 module_of ✅；adapter + NullGraphAdapter + Protocol（§4）→ Task1 ✅；retrieve enrich architecture 分支、chit-chat 不动（§4/§5）→ Task2 ✅；prompts 渲染 + 指引（§4/§5）→ Task3 ✅；best-effort/None 降级（§6）→ Task1 sqlite-error 测 + Task2 failure 测 + Task3 None 省略 ✅；E2E Q5（§7）→ Task4 ✅；只 enrich entry_candidates、不动 composite/门控/sse（§8）→ Task2 仅改 retrieve enrich、Task3 仅改 prompt ✅。

**2. 占位符扫描：** Task1-3 完整 before/after 代码 + 完整测试；Task4 E2E 给了具体 Q5 + 期望 + 回归项，复用既有服务器侧合成脚本模式。无 TBD/TODO。

**3. 类型一致性：** `module_of(entity_id:str)->str|None` 在 graph_adapter（实现）/ graph_factory（NullGraphAdapter 实现）/ retriever GraphProto（声明）三处签名一致；retriever enrich 调 `self.graph.module_of(eid)`、读 `c["module"]`；prompts 读 `c.get("module")`——键名 `module` 全程一致；候选 dict 仍 `{entity_id, summary_text, level, score, module}`（module 新增、向后兼容 .get）。
