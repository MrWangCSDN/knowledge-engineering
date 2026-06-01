# Neo4j 解耦 / 退役收尾 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development 或 superpowers:executing-plans。Steps 用 checkbox（`- [x]`）语法。

**Goal:** 让 QA 在 Neo4j 完全下线时仍正常工作（图导航走 CodeGraph、兜底走 Weaviate CodeEntity）——拆掉 startup 对 Neo4j 的两道硬闸，并修掉 Phase 1 引入的 `_REL_TO_KIND` fallback bug。

**Architecture:** CodeGraph 迁移（设计 [[CodeGraph-结构引擎集成-设计]] §7 退役清单）后，QA 图导航已不依赖 Neo4j，但 startup 没同步。本计划只做"解耦 + 修 bug"，不动 pipeline 写 Neo4j 的逻辑（写了没人读，无害，留到后续物理删）。

**Tech Stack:** Python · FastAPI startup · pytest。

**用户偏好:** Python 代码中文逐行注释。设计文档写 Obsidian、不双写仓库。

**探索已确认的事实（实现照此）:**
- **闸 1**：`src/service/api.py:_try_connect_backends`（L227-310）—— 没 `NEO4J_PASSWORD`（L269-270）或 Neo4j 连接失败（L278-280）都 `return None, None, None`，连带 Weaviate code/interp store 都不建。
- **闸 2**：startup 调用方（L175）`if neo4j_backend is not None and interp_store is not None:` —— neo4j_backend=None 时进 else（L182-192）把 weaviate stores 全清成 None + 挂 StubRetriever。
- QA 真正依赖的是 `interp_store`（+ `code_store` 兜底）；`build_retriever_for_project` / `build_tools_for_project` 已不取 `neo4j_backend`（Phase 1 已确认）。
- **bug**：`src/integrations/codegraph/graph_adapter.py:20` `_REL_TO_KIND = {None:"calls","calls":"calls","CALLS":"calls"}` + L63 `kind = _REL_TO_KIND.get(rel_type, "calls")` —— 未知 rel_type（如 `accesses_table`）fallback 到 `'calls'`，导致 `_extract_table_access` / `ke_table_access` 把 callees 误当表返回。应改成未知 rel_type 返 `[]`。
- **不在本计划**：方法→表血缘（接 MapperAccessIndex）——独立增强，pipeline 从没写过 accesses_table 边，非 Neo4j 退役回归。

---

## File Structure

| 文件 | 职责 | 动作 |
|---|---|---|
| `src/service/api.py` | `_try_connect_backends` 三后端独立；调用方闸改判 interp_store | Modify |
| `src/integrations/codegraph/graph_adapter.py` | 未知 rel_type 返 []（不再 fallback calls） | Modify |
| `src/service/infra_health.py` | Neo4j 降级为 optional（down 不致 unhealthy） | Modify |
| `tests/test_auth/test_startup_neo4j_optional.py` | startup 在 Neo4j 失败时仍建 Weaviate store | Create |
| `tests/test_auth/test_graph_adapter_unknown_rel.py` | 未知 rel_type 返 [] | Create |

---

## Task N-T1：startup 与 Neo4j 解耦（拆两道闸）

**Files:** Modify `src/service/api.py`；Test `tests/test_auth/test_startup_neo4j_optional.py`

- [x] **Step 1: 写失败测试**

```python
# tests/test_auth/test_startup_neo4j_optional.py
"""验证 Neo4j 连不上时，_try_connect_backends 仍返回 Weaviate code/interp store（不再 None,None,None）。"""
from unittest.mock import patch, MagicMock
import src.service.api as api


def test_neo4j_failure_keeps_weaviate_stores(monkeypatch):
    monkeypatch.setenv("WEAVIATE_URL", "http://x:8080")
    monkeypatch.setenv("WEAVIATE_API_KEY", "k")
    monkeypatch.setenv("NEO4J_PASSWORD", "p")          # 有密码，但连接会抛错
    # Neo4j 后端构造即抛 → 模拟 Neo4j 不可用
    with patch("src.knowledge.graph_neo4j.Neo4jGraphBackend", side_effect=RuntimeError("neo4j down")), \
         patch("src.knowledge.vector_store_weaviate.WeaviateVectorStore", return_value=MagicMock()) as mc, \
         patch("src.knowledge.weaviate_interpretation_store.WeaviateTopologicalInterpretStore", return_value=MagicMock()) as mi:
        neo4j_backend, code_store, interp_store = api._try_connect_backends()
    assert neo4j_backend is None, "Neo4j 连不上应返回 None"
    assert code_store is not None, "Neo4j 失败不应连累 code_store"
    assert interp_store is not None, "Neo4j 失败不应连累 interp_store"
```

- [x] **Step 2: 运行确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && python -m pytest tests/test_auth/test_startup_neo4j_optional.py -v`
Expected: FAIL（当前 Neo4j 失败 → return None,None,None → code_store/interp_store 都是 None）

- [x] **Step 3: 改 `_try_connect_backends`（闸 1）**

把 `# ─── 1) Neo4j ───` 整段（约 L258-280）改为"失败仅置 None、不 return"：
```python
    # ─── 1) Neo4j（可选；CodeGraph 迁移后图导航不依赖它，连不上不影响 QA）───
    # 设计 [[CodeGraph-结构引擎集成-设计]] §7：Neo4j 退役中。这里仅向后兼容保留连接尝试，
    # 失败只把 neo4j_backend 置 None，不再短路整个后端连接（否则会连累 Weaviate）。
    neo4j_backend = None                                   # 先置 None，连成功再覆盖
    try:
        from src.knowledge.graph_neo4j import Neo4jGraphBackend
        neo4j_uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
        neo4j_user = os.environ.get("NEO4J_USER", "neo4j")
        neo4j_password = os.environ.get("NEO4J_PASSWORD")
        neo4j_database = os.environ.get("NEO4J_DATABASE", "neo4j")
        if not neo4j_password:
            # 没密码就跳过（不再 return）——图导航走 CodeGraph
            _log.info("[startup] NEO4J_PASSWORD 未设 → 跳过 Neo4j（图导航走 CodeGraph，不影响 QA）")
        else:
            neo4j_backend = Neo4jGraphBackend(
                uri=neo4j_uri, user=neo4j_user, password=neo4j_password, database=neo4j_database
            )
            _ = neo4j_backend.node_count()                 # 轻量探活
            _log.info("[startup] Neo4j 连接成功: %s", neo4j_uri)
    except Exception as e:
        # 连不上只告警 + 置 None，继续往下连 Weaviate
        _log.warning("[startup] Neo4j 连接失败（图导航走 CodeGraph，不影响 QA）: %s", e)
        neo4j_backend = None
```
（`# ─── 2) Weaviate ───` 段不变，结尾仍 `return neo4j_backend, code_store, interp_store`。）

- [x] **Step 4: 改调用方闸（闸 2）**

`src/service/api.py` L175 把：
```python
    if neo4j_backend is not None and interp_store is not None:
```
改为（QA 只需 interp_store；neo4j_backend 可为 None）：
```python
    # QA 图导航走 CodeGraph，不再要求 neo4j_backend；只要 interp_store 在就用真实 per-request retriever
    if interp_store is not None:
```
（`app.state.neo4j_backend = neo4j_backend` 原样保留——可能为 None，向后兼容；下游已不依赖它。）

- [x] **Step 5: 运行确认通过**

Run: `python -m pytest tests/test_auth/test_startup_neo4j_optional.py -v`
Expected: PASS

- [x] **Step 6: 跑回归（startup/api 相关测试不破）**

Run: `python -m pytest tests/test_auth/ -k "startup or api or backend" -q`
Expected: 全绿（或与改动前同样的已知 skip）

- [x] **Step 7: 提交**

```bash
git add src/service/api.py tests/test_auth/test_startup_neo4j_optional.py
git commit -m "fix(startup): decouple Weaviate stores from Neo4j availability (Neo4j retirement)"
```

---

## Task N-T2：修 `_REL_TO_KIND` fallback bug（未知 rel_type 返 []）

**Files:** Modify `src/integrations/codegraph/graph_adapter.py`；Test `tests/test_auth/test_graph_adapter_unknown_rel.py`

- [x] **Step 1: 写失败测试**

```python
# tests/test_auth/test_graph_adapter_unknown_rel.py
"""验证未知 rel_type（如 accesses_table）不再被误当 'calls'，而是返回 []。"""
from src.integrations.codegraph.graph_adapter import CodeGraphGraphAdapter


class _FakeDB:
    # successors/predecessors 若被以 'calls' 调到，就返回非空 → 用来暴露 bug
    def successors(self, node_id, kind="calls"):
        return [type("N", (), {"qualified_name": "X::y#()", "kind": "method", "signature": "()"})()] if kind == "calls" else []
    def predecessors(self, node_id, kind="calls"):
        return []
    def find_nodes_by_qualified_name(self, qn):
        return [type("N", (), {"id": "nid", "qualified_name": qn, "kind": "method", "signature": "()"})()]


def test_unknown_rel_type_returns_empty():
    adp = CodeGraphGraphAdapter(_FakeDB())
    # accesses_table 是未知边 → 应返回 []（不能 fallback 到 calls 把 callee 当表）
    assert adp.successors("X::y#()", rel_type="accesses_table") == []
    # calls 仍正常
    assert adp.successors("X::y#()", rel_type="calls") != []
```

- [x] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_auth/test_graph_adapter_unknown_rel.py -v`
Expected: FAIL（accesses_table 当前 fallback 到 calls → 返回非空）

- [x] **Step 3: 改 `_walk` 的 kind 解析**

`src/integrations/codegraph/graph_adapter.py` L63 把：
```python
        kind = _REL_TO_KIND.get(rel_type, "calls")
```
改为（未知 rel_type → None → 直接返 []，不查图）：
```python
        # 未知 rel_type（如 accesses_table，CodeGraph 无此边）返回 None → 不查图、直接 []，
        # 避免 fallback 到 'calls' 把 callees 误当成该类型边返回（修 Phase 1 引入的 bug）
        kind = _REL_TO_KIND.get(rel_type, None)
        if kind is None:
            return []
```

- [x] **Step 4: 运行确认通过 + 回归**

Run: `python -m pytest tests/test_auth/test_graph_adapter_unknown_rel.py tests/ -k "graph_adapter or codegraph" -q`
Expected: PASS（新测过 + 既有 CodeGraph adapter 测试不破）

- [x] **Step 5: 提交**

```bash
git add src/integrations/codegraph/graph_adapter.py tests/test_auth/test_graph_adapter_unknown_rel.py
git commit -m "fix(codegraph): unknown rel_type returns [] instead of falling back to calls"
```

---

## Task N-T3：infra_health 把 Neo4j 降级为 optional

**Files:** Modify `src/service/infra_health.py`（先读现状再改）

- [x] **Step 1: 读现状** —— `grep -nE "neo4j|Neo4j|healthy|status|degraded" src/service/infra_health.py`，确认 Neo4j 当前是否计入"整体健康"。
- [x] **Step 2: 若 Neo4j down 会让整体 unhealthy** —— 改成 Neo4j 作为 optional 组件单独上报（如 `neo4j: "retired/optional"`），不计入致命健康判定；Weaviate/MySQL/CodeGraph 才是关键依赖。加/改对应测试（红→绿）。
- [x] **Step 3: 提交** `git commit -m "chore(health): treat Neo4j as optional (retiring)"`

---

## Task N-T4：端到端验证（Neo4j 不可达时 QA 仍通）

- [x] **Step 1:** 复用 `/tmp/cg_2c3_e2e.py`，临时 `unset NEO4J_PASSWORD`（或指向坏 host）后，走 startup 真路径起一次（或直接断言 `_try_connect_backends` 在 Neo4j 失败时仍返回 code/interp store 非 None）。
- [x] **Step 2:** 确认中文问题仍能召回 qualified_name 候选 + CodeGraph 图导航返回真实方法（与 2c.3 同样的判定）。
- [x] **Step 3:** 记录结果。

---

## Task N-T5：Obsidian 文档更新

- [x] 更新 `/Users/java/obsidian/01 Engineering/knowledge-engineering/CodeGraph-结构引擎集成-设计.md`：§7 退役清单标注"startup 已与 Neo4j 解耦、QA 不再依赖 Neo4j"；§12 加 N-T1/N-T2/N-T3 完成记录 + 移除原"遗留 1"。不双写仓库。

---

## Self-Review

**1. Spec 覆盖（§7 退役清单）：** startup 解耦（两道闸，N-T1）✅；图导航正确性 bug（N-T2）✅；健康检查降级（N-T3）✅；端到端验证（N-T4）✅；文档（N-T5）✅。pipeline 写 Neo4j 的物理删除明确不在本计划（写了无人读，无害）。

**2. 占位符扫描：** N-T1/N-T2 给了精确 before/after + 完整测试代码；N-T3 因 infra_health 现状未读，给了"先读再改"+ 判定原则（非占位，是真实待查）。

**3. 类型一致性：** 不新增类型；`_try_connect_backends` 返回签名不变 `(neo4j|None, code_store|None, interp_store|None)`，只是 Neo4j 失败时其余两个不再被连累。`_REL_TO_KIND.get(rel_type, None)` 改默认值，返回类型仍 `list[str]`。
