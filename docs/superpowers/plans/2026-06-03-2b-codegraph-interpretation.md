# 2b 解读重生（CodeGraph-backed）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 CodeGraph(.codegraph.db) 作数据源生成 mall-swarm 业务解读，`method_entity_id=qualified_name`，让"召回→C2 链路展开→代码片段"全链路 id 对齐。

**Architecture:** 新增 `CodeGraphFactsProvider` 把 CodeGraph 投影成 `StructureFacts`（entity.id=qualified_name、attrs 含 source_reader 片段/class_name/signature、relations=CALLS+CONTAINS）→ 注入**零改动复用**的 `TopologicalInterpreter(structure_facts=...)` → 写 `TopologicalInterpretation` tenant=mall-swarm。新增 CLI `run_codegraph_interpret.py`。

**Tech Stack:** Python 3.12、pydantic（StructureFacts）、sqlite3（CodeGraphDB 只读）、现有 TopologicalInterpreter / WeaviateTopologicalInterpretStore / source_reader。

**设计 spec:** Obsidian `[[2b解读重生-设计]]`（不在仓库；勿双写）。

---

## 已核对的精确契约（实现时照此，勿臆测）

**`src/models/structure.py`**（pydantic BaseModel）：
- `StructureEntity(id: str, type: EntityType, name: str, location: Optional[str]=None, module_id: Optional[str]=None, language: Optional[str]=None, attributes: dict=...)`
- `StructureRelation(type: RelationType, source_id: str, target_id: str, attributes: dict=...)` ← 注意类名是 **StructureRelation**
- `StructureFacts(entities: list[StructureEntity], relations: list[StructureRelation], meta: dict)`
- `EntityType.METHOD="method"`, `EntityType.CLASS="class"`；`RelationType.CALLS="calls"`, `RelationType.CONTAINS="contains"`

**`src/integrations/codegraph/db.py`**：
- `CgNode(id, kind, name, qualified_name, file_path, start_line, end_line, signature)`（dataclass）
- `CodeGraphDB(db_path)`；`.iter_method_nodes() -> list[CgNode]`；`.successors(node_id: str, kind="calls") -> list[CgNode]`（callees）；`.predecessors(node_id, kind="calls") -> list[CgNode]`（callers）
  - ⚠️ `successors/predecessors` 入参是 **CgNode.id**（CodeGraph 内部 id），返回的 CgNode 带 `.qualified_name`

**`src/integrations/codegraph/source_reader.py`**：`read_snippet(repo_root: str, file_path: str, start_line: int, end_line: int) -> str`

**`src/knowledge/topological_interpreter.py:47`**：`TopologicalInterpreter(structure_facts: StructureFacts, llm, weaviate_store, *, language="zh", embedding_dim=1024, max_workers=8, llm_timeout=90, repo_path="", layer_gate=1.0, max_retry_cycles=5, retry_delays=None, state_file=..., step_callback=None, progress_callback=None)`
- 内部读：`e.type==EntityType.METHOD`、`attrs["code_snippet"]`（**`_filter_meaningful` 要求非空，否则跳过**）、`attrs["class_name"]`、`attrs["signature"]`、`method.module_id`、`method.name`；`RelationType.CALLS`(建调用图)、`RelationType.CONTAINS`(target=method → source=class_entity_id)
- 写入：`store.add_with_created(vector, method_entity_id=method.id, interpretation_text, *, tenant, class_entity_id, class_name, method_name, signature, context_summary, language, related_entity_ids_json)`

**测试夹具**：`tests/` 下 CG-T1.1 建的 mini `.codegraph.db` builder（搜 `codegraph` + `conftest`/fixture 复用；若 fixture 名不确定，Task 1 Step 0 先 grep 定位）。

---

## File Structure

| 文件 | 职责 | 创建/修改 |
|---|---|---|
| `src/knowledge/codegraph_facts_provider.py` | CodeGraph → StructureFacts 投影（唯一新增主体，~150 行） | **创建** |
| `run_codegraph_interpret.py`（仓库根） | CLI：装配 FactsProvider → TopologicalInterpreter → run | **创建** |
| `tests/test_auth/test_codegraph_facts_provider.py` | FactsProvider 单测（mini fixture） | **创建** |
| `tests/test_auth/test_codegraph_interpret_cli.py` | CLI 装配单测（mock Provider+Interpreter） | **创建** |
| `tests/test_auth/test_codegraph_interpret_integration.py` | 集成：1-method facts → 解读器(mock LLM/spy store) → 写 qualified_name key | **创建** |

不改 `topological_interpreter.py`（只注入）。不动 Obsidian。

---

## Task 1: CodeGraphFactsProvider — CodeGraph → StructureFacts

**Files:**
- Create: `src/knowledge/codegraph_facts_provider.py`
- Test: `tests/test_auth/test_codegraph_facts_provider.py`

- [ ] **Step 0: 定位 mini .codegraph.db 测试夹具**

Run: `grep -rn "codegraph" tests/ | grep -iE "fixture|conftest|\.db|builder|tmp_path" | head`
记下夹具函数名/路径（下面测试用它建临时 .codegraph.db）。若夹具是 `def _build_mini_codegraph_db(path)` 之类，复用之；若无，本 Task 用 `sqlite3` 在测试里手建一张最小 nodes/edges 表（见 Step 1 备选）。

- [ ] **Step 1: 写失败测试 — 方法实体 id=qualified_name + 片段 + CALLS 关系 + 模块过滤**

```python
# tests/test_auth/test_codegraph_facts_provider.py
"""CodeGraphFactsProvider 单测：CodeGraph → StructureFacts，id=qualified_name。
设计 [[2b解读重生-设计]] §4.1。"""
import sqlite3
from src.models.structure import EntityType, RelationType
from src.knowledge.codegraph_facts_provider import CodeGraphFactsProvider


def _mini_db(path):
    """手建最小 .codegraph.db：2 个 method 节点 + 1 条 calls 边 + 1 个 mbg 方法。
    schema 对齐 src/integrations/codegraph/db.py 读取的列。"""
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE nodes (id TEXT, kind TEXT, name TEXT, qualified_name TEXT, "
                 "file_path TEXT, start_line INT, end_line INT, signature TEXT)")
    conn.execute("CREATE TABLE edges (source TEXT, target TEXT, kind TEXT, line INT, col INT)")
    # 业务层入口（mall-portal）
    conn.execute("INSERT INTO nodes VALUES "
                 "('n1','method','generateOrder','OmsPortalOrderController::generateOrder',"
                 "'mall-portal/src/main/java/Ctrl.java',10,20,'(OrderParam)')")
    # 业务层 Service（mall-portal）
    conn.execute("INSERT INTO nodes VALUES "
                 "('n2','method','generateOrder','OmsPortalOrderServiceImpl::generateOrder',"
                 "'mall-portal/src/main/java/Svc.java',30,60,'(OrderParam)')")
    # mbg 方法（应被 Phase1 module_filter 过滤掉，不单独成 entity）
    conn.execute("INSERT INTO nodes VALUES "
                 "('n3','method','insert','OmsOrderMapper::insert',"
                 "'mall-mbg/src/main/java/M.java',5,8,'(OmsOrder)')")
    conn.execute("INSERT INTO edges VALUES ('n1','n2','calls',12,4)")  # ctrl → svc
    conn.execute("INSERT INTO edges VALUES ('n2','n3','calls',40,8)")  # svc → mbg mapper
    conn.commit(); conn.close()


def test_build_facts_method_id_is_qualified_name(tmp_path):
    db = tmp_path / "x.codegraph.db"; _mini_db(db)
    repo = tmp_path / "repo"
    # 造源文件让 read_snippet 能读到（行 10-20 / 30-60）
    (repo / "mall-portal/src/main/java").mkdir(parents=True)
    (repo / "mall-portal/src/main/java/Ctrl.java").write_text("\n".join(f"line{i}" for i in range(60)))
    (repo / "mall-portal/src/main/java/Svc.java").write_text("\n".join(f"line{i}" for i in range(60)))

    provider = CodeGraphFactsProvider(db_path=str(db), repo_local_path=str(repo))
    facts = provider.build_structure_facts(
        module_filter={"mall-portal"}  # Phase1：只业务层
    )

    methods = [e for e in facts.entities if e.type == EntityType.METHOD]
    ids = {e.id for e in methods}
    # ① method 实体 id = qualified_name
    assert "OmsPortalOrderController::generateOrder" in ids
    assert "OmsPortalOrderServiceImpl::generateOrder" in ids
    # ② mbg 方法被 module_filter 过滤，不作为 entity
    assert "OmsOrderMapper::insert" not in ids
    # ③ 片段填充（attrs.code_snippet 非空）
    ctrl = next(e for e in methods if e.id == "OmsPortalOrderController::generateOrder")
    assert ctrl.attributes["code_snippet"].strip() != ""
    assert ctrl.attributes["class_name"] == "OmsPortalOrderController"
    assert ctrl.attributes["signature"] == "(OrderParam)"
    assert ctrl.module_id == "mall-portal"
    # ④ CALLS 关系按 qualified_name（含指向被过滤的 mbg 目标，供解读文本引用）
    calls = [(r.source_id, r.target_id) for r in facts.relations if r.type == RelationType.CALLS]
    assert ("OmsPortalOrderController::generateOrder", "OmsPortalOrderServiceImpl::generateOrder") in calls
    assert ("OmsPortalOrderServiceImpl::generateOrder", "OmsOrderMapper::insert") in calls
    # ⑤ CONTAINS：class → method（让解读器解析 class_entity_id）
    contains = [(r.source_id, r.target_id) for r in facts.relations if r.type == RelationType.CONTAINS]
    assert ("OmsPortalOrderController", "OmsPortalOrderController::generateOrder") in contains


def test_snippet_unreadable_yields_empty_not_crash(tmp_path):
    """源文件缺失 → code_snippet="" 不抛异常（解读器会 _filter_meaningful 跳过）。"""
    db = tmp_path / "x.codegraph.db"; _mini_db(db)
    provider = CodeGraphFactsProvider(db_path=str(db), repo_local_path=str(tmp_path / "norepo"))
    facts = provider.build_structure_facts(module_filter={"mall-portal"})
    ctrl = next(e for e in facts.entities
                if e.id == "OmsPortalOrderController::generateOrder")
    assert ctrl.attributes["code_snippet"] == ""  # 读不到→空，不崩
```

- [ ] **Step 2: 运行测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_auth/test_codegraph_facts_provider.py -q`
Expected: FAIL（`ModuleNotFoundError: codegraph_facts_provider` / `CodeGraphFactsProvider` 未定义）

- [ ] **Step 3: 实现 CodeGraphFactsProvider**

```python
# src/knowledge/codegraph_facts_provider.py
"""把 CodeGraph(.codegraph.db) 投影成 StructureFacts，供 TopologicalInterpreter 消费。

设计 [[2b解读重生-设计]] §4.1。核心价值：entity.id = CodeGraph qualified_name，
让重生的解读与 QA 召回/导航/代码片段同一套 id（消除 canonical_v1 ↔ qualified_name 漂移）。
"""
from __future__ import annotations

# 标准库 logging：记录读片段失败等诊断
import logging

# StructureFacts 三件套（pydantic 模型）+ 枚举
from src.models.structure import (
    StructureFacts,
    StructureEntity,
    StructureRelation,
    EntityType,
    RelationType,
)
# CodeGraph 只读访问层 + 读源码片段
from src.integrations.codegraph.db import CodeGraphDB, CgNode
from src.integrations.codegraph.source_reader import read_snippet

# 模块级 logger
_LOG = logging.getLogger(__name__)


class CodeGraphFactsProvider:
    """CodeGraph → StructureFacts 投影器（纯读，可单测）。"""

    def __init__(self, *, db_path: str, repo_local_path: str) -> None:
        """
        Args:
            db_path: .codegraph.db 路径
            repo_local_path: 源码仓库根（read_snippet 用）
        """
        # CodeGraphDB 只读句柄；repo 根用于按 file_path + 行号读片段
        self._db = CodeGraphDB(db_path)
        self._repo = repo_local_path

    @staticmethod
    def _module_of(file_path: str) -> str:
        """file_path 顶层目录 = 模块（mall-portal/mall-admin/...）。与 ML module_of 同口径。"""
        # split('/', 1)[0]：取第一个 '/' 前的部分；无 '/' 时返回整串
        return (file_path or "").split("/", 1)[0]

    @staticmethod
    def _class_qn(method_qn: str) -> str:
        """从 'Class::method' 取 'Class'（CONTAINS 的 source）。无 '::' 时返回原串。"""
        # rsplit('::', 1)[0]：从右切一刀取类全名部分
        return method_qn.rsplit("::", 1)[0] if "::" in method_qn else method_qn

    @staticmethod
    def _short_class_name(class_qn: str) -> str:
        """'com.x.OmsPortalOrderController' → 'OmsPortalOrderController'（短类名，attrs.class_name 用）。"""
        return class_qn.rsplit(".", 1)[-1]

    def _read_snippet_safe(self, node: CgNode) -> str:
        """读方法源码片段；任何异常（文件缺失/越界）→ 返回 ""（不中断批量）。"""
        try:
            return read_snippet(self._repo, node.file_path, node.start_line, node.end_line) or ""
        except Exception as e:  # noqa: BLE001 — best-effort，失败降级为空片段
            _LOG.debug("read_snippet 失败 %s: %s", node.qualified_name, e)
            return ""

    def build_structure_facts(self, *, module_filter: set[str] | None = None) -> StructureFacts:
        """遍历 CodeGraph method 节点，产出 StructureFacts。

        Args:
            module_filter: 只发射这些模块的 method 实体（Phase1 业务层白名单）；
                           None = 全量。被过滤模块仍可作为 CALLS 目标出现在 relations 里
                           （供上游解读文本引用），只是不单独成 entity / 不单独生成解读。
        Returns:
            StructureFacts（entity.id = qualified_name）
        """
        entities: list[StructureEntity] = []      # method + class 实体
        relations: list[StructureRelation] = []   # CALLS + CONTAINS
        seen_class_qns: set[str] = set()           # class 实体去重

        # 遍历所有 method 节点（CodeGraphDB 已只挑 kind='method'）
        for node in self._db.iter_method_nodes():
            module = self._module_of(node.file_path)
            # Phase1：模块白名单过滤（被过滤的方法不单独成 entity）
            if module_filter is not None and module not in module_filter:
                continue

            qn = node.qualified_name
            class_qn = self._class_qn(qn)

            # ① method 实体：id=qualified_name；attrs 填解读器读取的 key
            entities.append(StructureEntity(
                id=qn,
                type=EntityType.METHOD,
                name=node.name,
                module_id=module,
                attributes={
                    "code_snippet": self._read_snippet_safe(node),   # 解读器 _filter_meaningful 要求非空
                    "class_name": self._short_class_name(class_qn),  # 解读器 attrs["class_name"]
                    "signature": node.signature or "",               # 解读器 attrs["signature"]
                    "qualified_name": qn,
                },
            ))

            # ② CALLS 边：source/target 都投影成 qualified_name（与 entity.id 对齐）
            #    successors 入参是 CgNode.id（内部 id），返回的 callee 带 qualified_name
            for callee in self._db.successors(node.id, kind="calls"):
                relations.append(StructureRelation(
                    type=RelationType.CALLS,
                    source_id=qn,
                    target_id=callee.qualified_name,
                ))

            # ③ CONTAINS 边：class → method（解读器据此解析 class_entity_id）
            relations.append(StructureRelation(
                type=RelationType.CONTAINS,
                source_id=class_qn,
                target_id=qn,
            ))
            seen_class_qns.add(class_qn)

        # ④ 补 class 实体（去重；attrs.class_name 供解读器/写入用）
        for class_qn in seen_class_qns:
            entities.append(StructureEntity(
                id=class_qn,
                type=EntityType.CLASS,
                name=self._short_class_name(class_qn),
                attributes={"class_name": self._short_class_name(class_qn)},
            ))

        return StructureFacts(
            entities=entities,
            relations=relations,
            meta={"source": "codegraph", "module_filter": sorted(module_filter) if module_filter else "all"},
        )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_auth/test_codegraph_facts_provider.py -q`
Expected: PASS（2 passed）

- [ ] **Step 5: Commit**

```bash
git add src/knowledge/codegraph_facts_provider.py tests/test_auth/test_codegraph_facts_provider.py
git commit -m "feat(2b): CodeGraphFactsProvider — CodeGraph→StructureFacts(id=qualified_name)"
```

---

## Task 2: CLI `run_codegraph_interpret.py`（装配 Provider → TopologicalInterpreter）

**Files:**
- Create: `run_codegraph_interpret.py`（仓库根）
- Test: `tests/test_auth/test_codegraph_interpret_cli.py`

- [ ] **Step 1: 写失败测试 — 装配函数把 Provider 产出的 facts 注入 TopologicalInterpreter**

```python
# tests/test_auth/test_codegraph_interpret_cli.py
"""run_codegraph_interpret 装配单测：mock Provider + Interpreter，验证接线。"""
from unittest.mock import MagicMock, patch
from src.models.structure import StructureFacts
import run_codegraph_interpret as cli


def test_build_and_run_wires_facts_into_interpreter():
    fake_facts = StructureFacts(entities=[], relations=[], meta={})
    with patch.object(cli, "CodeGraphFactsProvider") as P, \
         patch.object(cli, "TopologicalInterpreter") as I, \
         patch.object(cli, "_build_llm", return_value=MagicMock()), \
         patch.object(cli, "_build_store", return_value=MagicMock()):
        P.return_value.build_structure_facts.return_value = fake_facts
        I.return_value.run.return_value = {"ok": 1}

        cli.build_and_run(
            db_path="x.db", repo_path="/opt/mall-swarm",
            project_id="mall-swarm", modules={"mall-portal"},
            workers=4,
        )

        # Provider 用正确路径构造 + module_filter 透传
        P.assert_called_once_with(db_path="x.db", repo_local_path="/opt/mall-swarm")
        P.return_value.build_structure_facts.assert_called_once_with(module_filter={"mall-portal"})
        # Interpreter 用 provider 产出的 facts 构造，且 repo_path/workers 透传
        _, kwargs = I.call_args
        assert kwargs["structure_facts"] is fake_facts
        assert kwargs["repo_path"] == "/opt/mall-swarm"
        assert kwargs["max_workers"] == 4
        I.return_value.run.assert_called_once()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_auth/test_codegraph_interpret_cli.py -q`
Expected: FAIL（`No module named 'run_codegraph_interpret'`）

- [ ] **Step 3: 实现 CLI**

```python
# run_codegraph_interpret.py（仓库根，仿 run_topological_interpret.py）
"""2b 解读重生 CLI：以 CodeGraph 为源生成业务解读（id=qualified_name）。

设计 [[2b解读重生-设计]] §4.3。与 run_topological_interpret.py（StructureFacts 源）物理隔离，
防误用老的 canonical_v1 源。

用法（服务器侧，需授权执行）：
  ./venv/bin/python run_codegraph_interpret.py \
      --project-id mall-swarm --repo-path /opt/mall-swarm \
      --codegraph-db /opt/mall-swarm/.codegraph/mall-swarm.codegraph.db \
      --modules mall-admin,mall-portal,mall-common,mall-search,mall-gateway \
      --workers 8
"""
from __future__ import annotations

import argparse
import os

from src.knowledge.codegraph_facts_provider import CodeGraphFactsProvider
from src.knowledge.topological_interpreter import TopologicalInterpreter


def _build_llm():
    """构造 LLM provider（复用主仓默认 provider 工厂）。"""
    # 复用现有 LLM 工厂；与 topological 跑批同源（DashScope/OpenAI 兼容）
    from src.llm.factory import build_default_llm  # type: ignore
    return build_default_llm()


def _build_store():
    """构造 WeaviateTopologicalInterpretStore（连接参数走 env，同 startup）。"""
    from src.knowledge.weaviate_interpretation_store import WeaviateTopologicalInterpretStore
    return WeaviateTopologicalInterpretStore(
        url=os.environ.get("WEAVIATE_URL", "http://localhost:8080"),
        api_key=os.environ.get("WEAVIATE_API_KEY") or None,
        grpc_port=int(os.environ.get("WEAVIATE_GRPC_PORT", "50051")),
        dimension=int(os.environ.get("WEAVIATE_DIMENSION", "1024")),
    )


def build_and_run(*, db_path: str, repo_path: str, project_id: str,
                  modules: set[str] | None, workers: int) -> dict:
    """装配 FactsProvider → TopologicalInterpreter → run。返回统计。"""
    # 1. CodeGraph → StructureFacts（id=qualified_name）
    provider = CodeGraphFactsProvider(db_path=db_path, repo_local_path=repo_path)
    facts = provider.build_structure_facts(module_filter=modules)
    # 2. 注入零改动复用的拓扑解读器；tenant 通过 store 的 add_with_created(tenant=) 走，
    #    这里 store 由 _build_store 提供，project_id 作 tenant 在解读器内部透传
    interp = TopologicalInterpreter(
        structure_facts=facts,
        llm=_build_llm(),
        weaviate_store=_build_store(),
        repo_path=repo_path,
        max_workers=workers,
        # tenant：TopologicalInterpreter 写入时需 project_id；通过 store 包装或解读器参数透传
        # （实现时确认解读器如何取 tenant：若它从 store 默认 tenant 取，则 store 构造时绑定 project_id）
    )
    return interp.run()


def main() -> None:
    ap = argparse.ArgumentParser(description="2b CodeGraph-backed 解读重生")
    ap.add_argument("--project-id", required=True)
    ap.add_argument("--repo-path", required=True)
    ap.add_argument("--codegraph-db", required=True)
    ap.add_argument("--modules", default="", help="逗号分隔模块白名单；空=全量")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    modules = {m.strip() for m in args.modules.split(",") if m.strip()} or None
    stats = build_and_run(
        db_path=args.codegraph_db, repo_path=args.repo_path,
        project_id=args.project_id, modules=modules, workers=args.workers,
    )
    print(stats)


if __name__ == "__main__":
    main()
```

> ⚠️ **实现注意（tenant 透传）**：`TopologicalInterpreter` 写入走 `store.add_with_created(..., tenant=?)`。实现 Step 3 前先 `grep -n "add_with_created\|tenant" src/knowledge/topological_interpreter.py` 确认解读器如何拿 tenant：
> - 若解读器内部固定不传 tenant → 需让 `_build_store()` 返回的 store 默认绑定 project_id（或给解读器加 tenant 参数——但这违反"不改解读器"，故优先在 store 侧绑定）。
> - 若解读器已有 tenant/project_id 入参 → CLI 透传 `project_id`。
> 这是本计划唯一需要"读后定接法"的点；用上面 grep 结果二选一，**不要留 placeholder**，确定后写死。

- [ ] **Step 4: 运行测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_auth/test_codegraph_interpret_cli.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add run_codegraph_interpret.py tests/test_auth/test_codegraph_interpret_cli.py
git commit -m "feat(2b): run_codegraph_interpret CLI (assemble FactsProvider→TopologicalInterpreter)"
```

---

## Task 3: 集成测试 — facts(id=qualified_name) → 解读器 → 写 qualified_name key

**Files:**
- Test: `tests/test_auth/test_codegraph_interpret_integration.py`

- [ ] **Step 1: 写测试 — 1-method facts 跑通解读器(mock LLM + spy store)，断言写入 key=qualified_name**

```python
# tests/test_auth/test_codegraph_interpret_integration.py
"""集成：手建 1-method StructureFacts(id=qualified_name) → TopologicalInterpreter
(mock LLM + spy store) → 断言 add_with_created(method_entity_id=qualified_name)。"""
from unittest.mock import MagicMock
from src.models.structure import StructureFacts, StructureEntity, StructureRelation, EntityType, RelationType
from src.knowledge.topological_interpreter import TopologicalInterpreter


def test_interpreter_writes_qualified_name_key(tmp_path):
    facts = StructureFacts(
        entities=[
            StructureEntity(id="OmsPortalOrderServiceImpl::generateOrder", type=EntityType.METHOD,
                            name="generateOrder", module_id="mall-portal",
                            attributes={"code_snippet": "public CommonResult generateOrder(){...}",
                                        "class_name": "OmsPortalOrderServiceImpl", "signature": "(OrderParam)"}),
            StructureEntity(id="OmsPortalOrderServiceImpl", type=EntityType.CLASS,
                            name="OmsPortalOrderServiceImpl", attributes={"class_name": "OmsPortalOrderServiceImpl"}),
        ],
        relations=[StructureRelation(type=RelationType.CONTAINS,
                                     source_id="OmsPortalOrderServiceImpl",
                                     target_id="OmsPortalOrderServiceImpl::generateOrder")],
        meta={},
    )
    # mock LLM：generate 返回固定业务解读
    llm = MagicMock()
    llm.generate.return_value = "该方法是下单主链路入口，校验并落库订单。"
    # spy store：记录 add_with_created 入参；list_existing_method_ids 返回空（无孤儿）
    store = MagicMock()
    store.list_existing_method_ids.return_value = []
    store.add_with_created.return_value = (True, True)

    interp = TopologicalInterpreter(
        structure_facts=facts, llm=llm, weaviate_store=store,
        repo_path=str(tmp_path), max_workers=1, layer_gate=0.0, max_retry_cycles=1,
        state_file=str(tmp_path / "state.json"),
    )
    interp.run()

    # 断言：写入的 method_entity_id 是 qualified_name
    assert store.add_with_created.called
    # add_with_created(vector, method_entity_id, interpretation_text, *, ...)：method_entity_id 是第 2 位置参或 kwarg
    called = store.add_with_created.call_args
    mid = called.kwargs.get("method_entity_id") or (called.args[1] if len(called.args) > 1 else None)
    assert mid == "OmsPortalOrderServiceImpl::generateOrder"
```

- [ ] **Step 2: 运行测试**

Run: `./venv/bin/python -m pytest tests/test_auth/test_codegraph_interpret_integration.py -q`
Expected: 大概率 PASS（facts→解读器→写 key）。**若失败**（如 LLM mock 的 `generate` 签名/返回不匹配、embedding 调用、或 run() 内部依赖未 mock）：
- 按报错 grep `topological_interpreter.py` 对应处（如 `self.llm.generate(` 的真实签名、是否调 `get_embedding`），补 mock（如 `patch("src.semantic.embedding.get_embedding", return_value=[0.1]*1024)`）。
- 若 `run()` 的线程/重试/embedding 依赖过重难 mock → **降级**：本测试改为只断言"facts.entities[0].id == qualified_name 且解读器 `_build_indices` 后 `self._methods` 含该 id"（直接调 `interp._build_indices()` 后查 `interp._methods`），把"写 key"留到 Task 4 服务器 E2E 验证。

- [ ] **Step 3: Commit**

```bash
git add tests/test_auth/test_codegraph_interpret_integration.py
git commit -m "test(2b): integration — CodeGraph facts → interpreter writes qualified_name key"
```

---

## Task 4: 回归 + （Phase 1）服务器重灌 + E2E 【需用户授权，不自动执行】

> 以下涉及**服务器侧执行 + LLM 批量 + 部署**，按本项目惯例**需用户每次显式授权**，不在 subagent 自动执行范围。

- [ ] **Step 1: 本地全量回归**（可自动）

Run: `./venv/bin/python -m pytest tests/test_auth tests/test_knowledge -q`
Expected: 全绿（新增 3 测试通过，无回归；基线 772+）

- [ ] **Step 2:（授权后）部署代码到服务器**

push release-0513 → 服务器 `git pull` → 重启 ke-api → `/health` healthy（沿用本项目部署流程）。

- [ ] **Step 3:（授权后）Phase 1 重灌 mall-swarm 业务层**

服务器侧（`/opt/knowledge-engineering`，source .env）：
```bash
./venv/bin/python run_codegraph_interpret.py \
  --project-id mall-swarm --repo-path /opt/mall-swarm \
  --codegraph-db <mall-swarm .codegraph.db 路径> \
  --modules mall-admin,mall-portal,mall-common,mall-search,mall-gateway \
  --workers 8
```
（先 grep 确认服务器上 .codegraph.db 实际路径；run_codegraph_interpret 跑完打印写入统计。）

- [ ] **Step 4:（授权后）E2E 验收**

- 召回探针：`search_method_hits_by_text("下单流程", "mall-swarm", limit=15)` 命中**解读库**（非 CodeEntity 兜底），generateOrder 解读进 top-N。
- 前端无痕问"下单流程是怎么实现的，用流程图展示" → 召回 interp → C2 链路展开 → 产出 call_chain → **ReactFlow 出图**。
- 同步 Obsidian `[[2b解读重生-设计]]` §实施完成标记 + `[[召回链路缺陷诊断与修复方案]]` Layer 1 标记已闭环。

---

## Phase 2（全量补齐）【follow-up，需单独授权】

Phase 1 验证召回/出图 OK 后：去掉 `--modules` 白名单（或扩到含 mbg 非 getter）跑全量。用 TopologicalInterpreter 的分层门禁 + 退避重试（`max_retry_cycles`/`retry_delays`）长跑。评估 LLM 成本/时间后再启动。

---

## Self-Review（写计划后自查）

- **Spec 覆盖**：§4.1 FactsProvider→Task1；§4.3 CLI→Task2；§5 id 对齐→Task1(id=qn)+Task3(写 key)；§6 分期→Task4 Phase1 模块白名单 + Phase2；§7 降级（片段读不到）→Task1 Step1 第 2 测试 + `_read_snippet_safe`；§8 测试→Task1/2/3。✓
- **Placeholder**：唯一"读后定接法"点是 Task2 tenant 透传（已给明确 grep + 二选一指令，要求写死不留 placeholder）；Task3 给了失败降级路径。无 TBD。
- **类型一致**：`StructureRelation`（非 Relation）、`EntityType.METHOD/CLASS`、`RelationType.CALLS/CONTAINS`、`CodeGraphDB.successors(node_id,kind)`、`read_snippet(repo,file,start,end)`、`add_with_created(...,method_entity_id=...,tenant=...)` —— 各 Task 一致，与"已核对契约"节对齐。
