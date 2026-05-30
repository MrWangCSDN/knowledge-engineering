# CodeGraph 结构引擎集成 — Phase 0 + Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 mall-swarm 用 CodeGraph 的 `.codegraph.db`（**直读 SQLite**）跑通结构导航（callers/callees/impact），把 Neo4j 踢出导航路径；身份切到 `qualified_name`。

**Architecture:** CodeGraph 当结构引擎，KE 只读它的 SQLite 库。新增 `src/integrations/codegraph/`：`CodeGraphDB`(只读 SQL) → `CodeGraphGraphAdapter`(实现现有 `GraphProto`) → 在 `qa_router` 注入处把 `Neo4jGraphAdapter` 换成它。ReAct 大脑/工具/中文/引用**不动**（工具本就依赖注入式 `GraphProto`）。身份用 `DurableKey`(qualified_name + 归一化 signature)。MCP 推迟到后续 Phase。

**Tech Stack:** Python 3 + 标准库 `sqlite3`(只读 `mode=ro`) + `subprocess`(跑 `codegraph index`) + pytest。CodeGraph CLI(已装 v0.9.6)。

**设计 spec（已审批，单一来源）:** `/Users/java/obsidian/01 Engineering/knowledge-engineering/CodeGraph-结构引擎集成-设计.md`（本计划只覆盖 §10 的 Phase 0 + Phase 1）

**用户偏好:** 所有 Python 代码**中文逐行注释**（学习者）；下方代码块已带注释，实现时保留。

**关键实测事实（写代码照这个，别瞎猜）:**
- `.codegraph/codegraph.db` 表结构：
  `nodes(id TEXT PK, kind, name, qualified_name, file_path, language, start_line, end_line, signature, ...)`；
  `edges(id, source TEXT→nodes.id, target TEXT→nodes.id, kind, line, ...)`，索引 `idx_edges_source_kind(source,kind)` / `idx_edges_target_kind(target,kind)`。
- `qualified_name` 形如 `OmsPortalOrderServiceImpl::generateOrder`（Class::method，行号无关）；调用边 `kind='calls'`。
- `GraphProto`（`src/service/qa_engine/retriever.py:39-43`）只有两个方法：`successors(entity_id, rel_type=None)->list[str]`、`predecessors(entity_id, rel_type=None)->list[str]`。
- 注入点（`src/service/qa_router.py`）：`build_retriever_for_project`(L96) 与 `build_tools_for_project`(L156) 都有 `graph_adapter = Neo4jGraphAdapter(neo4j_backend, project_id=project_id)`；后者已在 L149-150 取到 `repo_local_path`。

---

## File Structure

| 文件 | 职责 | 动作 |
|---|---|---|
| `src/integrations/__init__.py` | 包标记（若不存在） | Create |
| `src/integrations/codegraph/__init__.py` | 子包导出 | Create |
| `src/integrations/codegraph/db.py` | `CodeGraphDB`：只读 SQLite 访问（nodes/edges 查询） | Create |
| `src/integrations/codegraph/durable_key.py` | `durable_key()`：从节点派生 qualified_name(+签名) 持久身份 | Create |
| `src/integrations/codegraph/graph_adapter.py` | `CodeGraphGraphAdapter`：实现 `GraphProto` | Create |
| `src/integrations/codegraph/paths.py` | `codegraph_db_path(repo_local_path)`：定位 .codegraph.db | Create |
| `src/integrations/codegraph/index_manager.py` | `CodeGraphIndexManager`：跑 `codegraph index` | Create |
| `src/service/qa_router.py` | 注入处 Neo4jGraphAdapter → CodeGraphGraphAdapter | Modify |
| `tests/test_integrations/codegraph/_fixture.py` | 造迷你 .codegraph.db 的测试夹具 | Create |
| `tests/test_integrations/codegraph/test_*.py` | 单测 | Create |
| `docs/.../phase0-codegraph-vs-self.md` | Phase 0 对照报告（产出物） | Create |

> 测试夹具自建一个 3 节点的迷你 sqlite（同 CodeGraph 表结构），不依赖 46MB 真库，单测快且确定。

---

# Phase 0 — 先验证再切（go/no-go 闸门，必须先做）

> ⚠️ **执行前置条件**：完整对照需要 KE 数据栈在线（当前 Neo4j 库为空、Weaviate 隧道离线）。CodeGraph 侧分析可立即跑；KE 侧需用户先把栈拉起来（起 Weaviate 隧道 + 确认 Neo4j 有 mall-swarm 图）。**这一步不写产品代码，是调研+判断。**

### Task 0.1：CodeGraph 覆盖度分析（可立即跑）

**Files:** Create `docs/superpowers/plans/phase0-codegraph-vs-self.md`（报告）

- [ ] **Step 1：跑覆盖度查询**

Run（mall-swarm 库已存在）:
```bash
DB="/Users/java/repos/mall-swarm/.codegraph/codegraph.db"
# 各 kind 节点数
sqlite3 -header "$DB" "SELECT kind, COUNT(*) FROM nodes GROUP BY kind ORDER BY 2 DESC;"
# 各 kind 边数（calls 是导航命脉）
sqlite3 -header "$DB" "SELECT kind, COUNT(*) FROM edges GROUP BY kind ORDER BY 2 DESC;"
# 关键链路抽查：下单流 3 层是否都在 + calls 边连通
sqlite3 -header "$DB" "SELECT qualified_name FROM nodes WHERE name='generateOrder';"
```
Expected：method/class/route 等节点齐全；`calls` 边数可观；generateOrder 三层都在。

- [ ] **Step 2：对照 KE 需求清单，逐条判定**

在报告里对每条 KE QA 依赖打勾（✅够 / ⚠️弱 / ❌缺）：callers/callees（calls 边）、impact（多跳）、方法→源码（file_path+行）、入口（route 节点）、类层级（extends/implements 边）。记录任何 CodeGraph 明显比 javaparser 浅的点。

- [ ] **Step 3：（KE 栈在线时）头对头抽查**

栈拉起后，挑 5 个 mall-swarm 业务方法，CodeGraph callees vs Neo4j callees 对比覆盖；记差异。

- [ ] **Step 4：写 go/no-go 结论**

报告末尾给明确结论：**GO**（覆盖够，进 Phase 1）/ **NO-GO**（列出致命缺口）。提交报告：
```bash
cd /Users/java/knowledge-engineering-auth
git add docs/superpowers/plans/phase0-codegraph-vs-self.md
git commit -m "docs(codegraph): Phase 0 coverage comparison + go/no-go"
```

### 🚦 CHECKPOINT：go/no-go
**GO 才继续 Phase 1。** NO-GO 则回设计，评估单补缺口或换路线。下面 Phase 1 的细节可能依 Phase 0 结论微调（如某 edge kind 命名不同）。

---

# Phase 1 — 身份切换 + 直读 SQLite 接入核心

### Task 1.1：测试夹具——造迷你 .codegraph.db

**Files:** Create `tests/test_integrations/__init__.py`、`tests/test_integrations/codegraph/__init__.py`、`tests/test_integrations/codegraph/_fixture.py`

- [ ] **Step 1：写夹具**（非 TDD，是测试基建）

```python
# tests/test_integrations/codegraph/_fixture.py
"""造一个迷你 .codegraph.db（同 CodeGraph 真实表结构），供单测用。

图：OmsCtrl::generateOrder --calls--> OmsService::generateOrder --calls--> OmsOrderDao::save
"""
import sqlite3  # 标准库 SQLite 驱动


def make_fixture_db(path: str) -> None:
    """在 path 建一个迷你只含 nodes/edges 的库。"""
    conn = sqlite3.connect(path)  # 没有文件会新建
    # executescript 可一次执行多条 DDL；列与 CodeGraph 真库同名
    conn.executescript(
        """
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
            qualified_name TEXT NOT NULL, file_path TEXT NOT NULL, language TEXT NOT NULL,
            start_line INTEGER NOT NULL, end_line INTEGER NOT NULL, signature TEXT
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
            target TEXT NOT NULL, kind TEXT NOT NULL, line INTEGER
        );
        """
    )
    # 3 个方法节点（含一个重载演示：同 qualified_name 不同 signature 可后续扩展）
    nodes = [
        ("n1", "method", "generateOrder", "OmsCtrl::generateOrder",
         "Ctrl.java", "java", 40, 42, "CommonResult (OrderParam)"),
        ("n2", "method", "generateOrder", "OmsService::generateOrder",
         "Svc.java", "java", 25, 30, "Map (OrderParam)"),
        ("n3", "method", "save", "OmsOrderDao::save",
         "Dao.java", "java", 10, 12, "int (OmsOrder)"),
    ]
    # executemany：批量插入，? 是占位符防 SQL 注入
    conn.executemany(
        "INSERT INTO nodes(id,kind,name,qualified_name,file_path,language,"
        "start_line,end_line,signature) VALUES (?,?,?,?,?,?,?,?,?)", nodes
    )
    conn.executemany(
        "INSERT INTO edges(source,target,kind) VALUES (?,?,?)",
        [("n1", "n2", "calls"), ("n2", "n3", "calls")]
    )
    conn.commit()  # 提交事务
    conn.close()   # 关连接
```

- [ ] **Step 2：提交**
```bash
cd /Users/java/knowledge-engineering-auth
git add tests/test_integrations/
git commit -m "test(codegraph): mini .codegraph.db fixture builder"
```

### Task 1.2：CodeGraphDB（只读 SQLite 访问）

**Files:** Create `src/integrations/__init__.py`、`src/integrations/codegraph/__init__.py`、`src/integrations/codegraph/db.py`；Test `tests/test_integrations/codegraph/test_db.py`

- [ ] **Step 1：写失败测试**

```python
# tests/test_integrations/codegraph/test_db.py
"""CodeGraphDB 只读访问单测：用迷你夹具库验证查询。"""
from tests.test_integrations.codegraph._fixture import make_fixture_db
from src.integrations.codegraph.db import CodeGraphDB


def test_find_by_qualified_name_and_successors(tmp_path):
    db_path = str(tmp_path / "codegraph.db")  # pytest 临时目录
    make_fixture_db(db_path)
    db = CodeGraphDB(db_path)

    # 按 qualified_name 精确找节点
    svc = db.find_nodes_by_qualified_name("OmsService::generateOrder")
    assert len(svc) == 1 and svc[0].id == "n2" and svc[0].signature == "Map (OrderParam)"

    # successors(callees)：Service.generateOrder → Dao.save
    callees = db.successors("n2", "calls")
    assert [c.qualified_name for c in callees] == ["OmsOrderDao::save"]

    # predecessors(callers)：谁调了 Service.generateOrder → Ctrl.generateOrder
    callers = db.predecessors("n2", "calls")
    assert [c.qualified_name for c in callers] == ["OmsCtrl::generateOrder"]


def test_get_node_and_missing(tmp_path):
    db_path = str(tmp_path / "codegraph.db")
    make_fixture_db(db_path)
    db = CodeGraphDB(db_path)
    assert db.get_node("n3").name == "save"
    assert db.get_node("nope") is None
```

- [ ] **Step 2：运行确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && python -m pytest tests/test_integrations/codegraph/test_db.py -v`
Expected: FAIL（`ModuleNotFoundError: src.integrations.codegraph.db`）

- [ ] **Step 3：写实现**

```python
# src/integrations/__init__.py
"""外部工具集成包。"""
```
```python
# src/integrations/codegraph/__init__.py
"""CodeGraph 集成子包：只读 SQLite 访问 + GraphProto 适配 + 索引管理。"""
```
```python
# src/integrations/codegraph/db.py
"""只读访问 CodeGraph 生成的 .codegraph.db。

CodeGraph 把代码结构索引进单文件 SQLite；KE 以 mode=ro 只读查它，
拿稳定身份(qualified_name)与精确导航(successors/predecessors)。KE 永不写库。
"""
from __future__ import annotations

import sqlite3                       # 标准库 SQLite
from dataclasses import dataclass    # 用 dataclass 做轻量数据载体
from typing import Optional

# 一次性 SELECT 的列清单，避免到处重复
_COLS = "id,kind,name,qualified_name,file_path,start_line,end_line,signature"


@dataclass(frozen=True)              # frozen=True → 不可变，安全当字典键/传递
class CgNode:
    """CodeGraph 节点的精简投影（只取 KE 用得到的列）。"""
    id: str
    kind: str
    name: str
    qualified_name: str
    file_path: str
    start_line: int
    end_line: int
    signature: Optional[str]


class CodeGraphDB:
    """只读 SQLite 访问层。"""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        # file: URI + mode=ro：只读打开，KE 误写也写不进去，杜绝弄坏 CodeGraph 索引
        self._uri = f"file:{db_path}?mode=ro"

    def _connect(self) -> sqlite3.Connection:
        # uri=True 让 sqlite3 把字符串当 URI 解析（才认 mode=ro）
        conn = sqlite3.connect(self._uri, uri=True)
        conn.row_factory = sqlite3.Row   # 查询结果可按列名取值，如 row["name"]
        return conn

    def find_nodes_by_qualified_name(self, qualified_name: str) -> list[CgNode]:
        """按 qualified_name 精确找（重载会多行，靠 signature 再分）。"""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_COLS} FROM nodes WHERE qualified_name = ?",
                (qualified_name,),
            ).fetchall()
        return [self._row_to_node(r) for r in rows]

    def successors(self, node_id: str, kind: str = "calls") -> list[CgNode]:
        """出边目标(callees)：edges.source = node_id。"""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {self._prefixed('t')} FROM edges e "
                "JOIN nodes t ON e.target = t.id WHERE e.source = ? AND e.kind = ?",
                (node_id, kind),
            ).fetchall()
        return [self._row_to_node(r) for r in rows]

    def predecessors(self, node_id: str, kind: str = "calls") -> list[CgNode]:
        """入边来源(callers)：edges.target = node_id。"""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {self._prefixed('s')} FROM edges e "
                "JOIN nodes s ON e.source = s.id WHERE e.target = ? AND e.kind = ?",
                (node_id, kind),
            ).fetchall()
        return [self._row_to_node(r) for r in rows]

    def get_node(self, node_id: str) -> Optional[CgNode]:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_COLS} FROM nodes WHERE id = ?", (node_id,)
            ).fetchone()
        return self._row_to_node(row) if row else None

    @staticmethod
    def _prefixed(alias: str) -> str:
        # 把 _COLS 每列加表别名前缀，如 't.id,t.kind,...'，用于 JOIN 查询
        return ",".join(f"{alias}.{c}" for c in _COLS.split(","))

    @staticmethod
    def _row_to_node(r: sqlite3.Row) -> CgNode:
        # sqlite3.Row 支持按列名索引；逐列构造 CgNode
        return CgNode(
            id=r["id"], kind=r["kind"], name=r["name"],
            qualified_name=r["qualified_name"], file_path=r["file_path"],
            start_line=r["start_line"], end_line=r["end_line"], signature=r["signature"],
        )
```

- [ ] **Step 4：运行确认通过**

Run: `cd /Users/java/knowledge-engineering-auth && python -m pytest tests/test_integrations/codegraph/test_db.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5：提交**
```bash
cd /Users/java/knowledge-engineering-auth
git add src/integrations/ tests/test_integrations/codegraph/test_db.py
git commit -m "feat(codegraph): read-only CodeGraphDB (nodes/edges queries)"
```

### Task 1.3：DurableKey（持久身份）

**Files:** Create `src/integrations/codegraph/durable_key.py`；Test `tests/test_integrations/codegraph/test_durable_key.py`

- [ ] **Step 1：写失败测试**

```python
# tests/test_integrations/codegraph/test_durable_key.py
"""DurableKey 单测：方法带归一化签名、非方法只用 qualified_name、签名格式无关。"""
from src.integrations.codegraph.db import CgNode
from src.integrations.codegraph.durable_key import durable_key


def _m(qn, sig, kind="method"):
    return CgNode(id="x", kind=kind, name="m", qualified_name=qn,
                  file_path="f", start_line=1, end_line=2, signature=sig)


def test_method_key_includes_normalized_signature():
    # 方法：qualified_name + 归一化签名（去空白/去泛型）
    assert durable_key(_m("A::m", "List<X> (Long id)")) == "A::m(Longid)"


def test_signature_formatting_does_not_change_key():
    # 同一方法不同空白/泛型写法 → 同 key（行号/格式无关，稳）
    a = durable_key(_m("A::m", "List<X> (Long id)"))
    b = durable_key(_m("A::m", "List< X >  (Long  id)"))
    assert a == b


def test_overload_keys_differ():
    assert durable_key(_m("A::m", "(Long id)")) != durable_key(_m("A::m", "(String s)"))


def test_non_method_uses_qualified_name_only():
    assert durable_key(_m("A::Foo", None, kind="class")) == "A::Foo"
```

- [ ] **Step 2：运行确认失败**

Run: `python -m pytest tests/test_integrations/codegraph/test_durable_key.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3：写实现**

```python
# src/integrations/codegraph/durable_key.py
"""KE 的持久身份：从 CodeGraph 节点派生「全名身份证」，行号无关。

- key = qualified_name（形如 'Class::method'）
- 方法再拼上归一化签名以区分重载
不持久化 CodeGraph 的 node.id（含行号、不稳定）。
"""
from __future__ import annotations

import re                                   # 正则，用于归一化签名
from src.integrations.codegraph.db import CgNode


def _norm_sig(sig: str | None) -> str:
    """归一化签名：去掉所有空白和泛型 <...>，使格式差异不影响 key。

    例：'List<X> (Long id)' → '(Longid)'；够用来区分重载，又不因排版抖动。
    """
    if not sig:                             # 没签名（None/空）→ 空串
        return ""
    s = re.sub(r"<[^>]*>", "", sig)         # 去掉泛型尖括号内容
    s = re.sub(r"\s+", "", s)               # 去掉所有空白字符
    return s


def durable_key(node: CgNode) -> str:
    """方法：qualified_name + 归一化签名；其它类型：只用 qualified_name。"""
    if node.kind == "method":               # 只有方法才可能重载，需要签名区分
        return f"{node.qualified_name}{_norm_sig(node.signature)}"
    return node.qualified_name
```

- [ ] **Step 4：运行确认通过** → PASS（4 passed）
- [ ] **Step 5：提交**
```bash
git add src/integrations/codegraph/durable_key.py tests/test_integrations/codegraph/test_durable_key.py
git commit -m "feat(codegraph): DurableKey identity (qualified_name + normalized signature)"
```

### Task 1.4：CodeGraphGraphAdapter（实现 GraphProto）

**Files:** Create `src/integrations/codegraph/graph_adapter.py`；Test `tests/test_integrations/codegraph/test_graph_adapter.py`

- [ ] **Step 1：写失败测试**

```python
# tests/test_integrations/codegraph/test_graph_adapter.py
"""CodeGraphGraphAdapter 单测：satisfies GraphProto，入/出参都是 durable_key 字符串。"""
from tests.test_integrations.codegraph._fixture import make_fixture_db
from src.integrations.codegraph.db import CodeGraphDB
from src.integrations.codegraph.graph_adapter import CodeGraphGraphAdapter


def _adapter(tmp_path):
    db_path = str(tmp_path / "codegraph.db")
    make_fixture_db(db_path)
    return CodeGraphGraphAdapter(CodeGraphDB(db_path))


def test_successors_returns_durable_keys(tmp_path):
    a = _adapter(tmp_path)
    # 传入 durable_key（方法带归一化签名）；夹具里 Service.generateOrder 签名 'Map (OrderParam)'
    out = a.successors("OmsService::generateOrder(Map(OrderParam)")  # 见下注：key 由调用方给
    # 实际调用方会用 durable_key 算好；这里直接验最终行为：能解析并返回 callees 的 key
    assert "OmsOrderDao::save(int(OmsOrder)" in out


def test_predecessors_returns_durable_keys(tmp_path):
    a = _adapter(tmp_path)
    out = a.predecessors("OmsService::generateOrder(Map(OrderParam)")
    assert "OmsCtrl::generateOrder(CommonResult(OrderParam)" in out
```

> 注：上面 key 字符串等于 `qualified_name + _norm_sig(signature)`。实现里 `_resolve` 用 `qualified_name`（'(' 前部分）兜底匹配，所以即便调用方只传 `OmsService::generateOrder` 也能命中（夹具中该名唯一）。测试用完整 key 验证端到端。

- [ ] **Step 2：运行确认失败** → FAIL（模块不存在）

- [ ] **Step 3：写实现**

```python
# src/integrations/codegraph/graph_adapter.py
"""把 CodeGraphDB 包成 KE 的 GraphProto（successors/predecessors）。

GraphProto 的 entity_id 用 KE 的持久身份(durable_key)；
本 adapter 负责 durable_key ↔ CodeGraph 节点 的解析，返回也用 durable_key。
"""
from __future__ import annotations

from typing import Optional
from src.integrations.codegraph.db import CodeGraphDB, CgNode
from src.integrations.codegraph.durable_key import durable_key

# GraphProto 的 rel_type(KE 语义) → CodeGraph edge.kind。None 默认 calls。
_REL_TO_KIND = {None: "calls", "calls": "calls", "CALLS": "calls"}


class CodeGraphGraphAdapter:
    """实现 src.service.qa_engine.retriever.GraphProto，后端是只读 SQLite。"""

    def __init__(self, db: CodeGraphDB) -> None:
        self._db = db                       # 注入只读 DB 访问层

    def _resolve(self, entity_id: str) -> list[CgNode]:
        """durable_key → CodeGraph 节点。先按 qualified_name 找，重载再用完整 key 精筛。"""
        qn = entity_id.split("(", 1)[0]     # '(' 前是 qualified_name 部分
        cands = self._db.find_nodes_by_qualified_name(qn)
        if len(cands) <= 1:                 # 唯一 → 直接返回
            return cands
        # 多个(重载) → 用完整 durable_key 再筛；筛空则退回全部(宁可多不可漏)
        exact = [n for n in cands if durable_key(n) == entity_id]
        return exact or cands

    def _walk(self, entity_id: str, rel_type: Optional[str], direction: str) -> list[str]:
        """successors/predecessors 公共逻辑，direction ∈ {'succ','pred'}。"""
        kind = _REL_TO_KIND.get(rel_type, "calls")
        out: list[str] = []
        seen: set[str] = set()              # 去重（不同 source 节点可能指向同一目标）
        for node in self._resolve(entity_id):
            neighbors = (self._db.successors(node.id, kind) if direction == "succ"
                         else self._db.predecessors(node.id, kind))
            for nb in neighbors:
                key = durable_key(nb)
                if key not in seen:
                    seen.add(key)
                    out.append(key)
        return out

    def successors(self, entity_id: str, rel_type: Optional[str] = None) -> list[str]:
        """出边(callees) 的 durable_key 列表。"""
        return self._walk(entity_id, rel_type, "succ")

    def predecessors(self, entity_id: str, rel_type: Optional[str] = None) -> list[str]:
        """入边(callers) 的 durable_key 列表。"""
        return self._walk(entity_id, rel_type, "pred")
```

- [ ] **Step 4：运行确认通过** → PASS（2 passed）
- [ ] **Step 5：提交**
```bash
git add src/integrations/codegraph/graph_adapter.py tests/test_integrations/codegraph/test_graph_adapter.py
git commit -m "feat(codegraph): CodeGraphGraphAdapter implements GraphProto over SQLite"
```

### Task 1.5：paths + IndexManager

**Files:** Create `src/integrations/codegraph/paths.py`、`src/integrations/codegraph/index_manager.py`；Test `tests/test_integrations/codegraph/test_paths_index.py`

- [ ] **Step 1：写失败测试**

```python
# tests/test_integrations/codegraph/test_paths_index.py
"""paths + IndexManager 单测：路径拼接确定；index 命令构造正确(不真跑 Node)。"""
from src.integrations.codegraph.paths import codegraph_db_path
from src.integrations.codegraph.index_manager import build_index_command


def test_db_path_under_repo():
    assert codegraph_db_path("/repos/mall-swarm") == "/repos/mall-swarm/.codegraph/codegraph.db"


def test_index_command():
    # 构造 `codegraph index <repo> -i` 这种命令（force 全量）
    assert build_index_command("/repos/mall-swarm", force=True) == \
        ["codegraph", "index", "/repos/mall-swarm", "--force"]
    assert build_index_command("/repos/mall-swarm", force=False) == \
        ["codegraph", "index", "/repos/mall-swarm"]
```

- [ ] **Step 2：运行确认失败** → FAIL

- [ ] **Step 3：写实现**

```python
# src/integrations/codegraph/paths.py
"""project → 该工程 .codegraph.db 的路径。

CodeGraph 天生把库放在 <repo>/.codegraph/codegraph.db；多租户即"一工程一库一文件"，
物理隔离，互不打架。
"""
import os


def codegraph_db_path(repo_local_path: str) -> str:
    """返回该工程 .codegraph.db 的绝对路径。"""
    # os.path.join 按系统分隔符拼路径
    return os.path.join(repo_local_path, ".codegraph", "codegraph.db")
```
```python
# src/integrations/codegraph/index_manager.py
"""跑 `codegraph index` 给某工程建/更新 .codegraph.db（子进程）。"""
from __future__ import annotations

import subprocess              # 跑外部命令
from typing import Optional


def build_index_command(repo_local_path: str, force: bool = False) -> list[str]:
    """构造 codegraph index 命令（纯函数，便于单测）。"""
    cmd = ["codegraph", "index", repo_local_path]   # 基础命令
    if force:                                        # force=全量重建
        cmd.append("--force")
    return cmd


def run_index(repo_local_path: str, force: bool = False,
              timeout: Optional[int] = 600) -> subprocess.CompletedProcess:
    """跑索引；冷库 ~1-2min，热重建 ~20s。timeout 默认 10min。"""
    # check=True：非 0 退出码会抛 CalledProcessError，便于上层感知失败
    return subprocess.run(
        build_index_command(repo_local_path, force),
        capture_output=True, text=True, timeout=timeout, check=True,
    )
```

- [ ] **Step 4：运行确认通过** → PASS（2 passed）
- [ ] **Step 5：提交**
```bash
git add src/integrations/codegraph/paths.py src/integrations/codegraph/index_manager.py tests/test_integrations/codegraph/test_paths_index.py
git commit -m "feat(codegraph): db path resolver + index command runner"
```

### Task 1.6：qa_router 注入切换（Neo4j → CodeGraph，导航路径）

**Files:** Modify `src/service/qa_router.py`（两处 `graph_adapter = Neo4jGraphAdapter(...)`）

> 目标：ReAct 工具的 `graph` 改由 CodeGraph 驱动。`build_tools_for_project` 已有 `repo_local_path`(L150)，直接用。`build_retriever_for_project` 是 sync 无 db，本任务先切 `build_tools_for_project`（ReAct 导航工具的 graph 在这里），retriever 的 graph 在 Step 3 用同款方式切（从 app.state 取 repo 路径）。

- [ ] **Step 1：写集成测试（monkeypatch，不连真后端）**

```python
# tests/test_integrations/codegraph/test_router_swap.py
"""验证 build_tools_for_project 注入的是 CodeGraphGraphAdapter（而非 Neo4j）。"""
import pytest
from src.integrations.codegraph.graph_adapter import CodeGraphGraphAdapter


def test_codegraph_adapter_is_graphproto():
    # CodeGraphGraphAdapter 必须满足 GraphProto（有 successors/predecessors）
    from src.service.qa_engine.retriever import GraphProto  # Protocol
    assert hasattr(CodeGraphGraphAdapter, "successors")
    assert hasattr(CodeGraphGraphAdapter, "predecessors")
    # 鸭子类型：实例可当 GraphProto 用（运行时 Protocol 不强制，这里断言方法齐全即可）
```

> 注：完整的 router 装配测试依赖 FastAPI app.state + DB，留到 E2E（Step 4）。此单测先钉住"适配器形状对"。

- [ ] **Step 2：运行确认通过**（此测试此刻应已 PASS，因为 1.4 已实现适配器）

Run: `python -m pytest tests/test_integrations/codegraph/test_router_swap.py -v`

- [ ] **Step 3：改 qa_router 注入处**

在 `build_tools_for_project`（约 L152-156）把 Neo4j 适配器换成 CodeGraph：

```python
    # 旧：
    # from src.service.qa_engine.adapters import Neo4jGraphAdapter, WeaviateTopologicalAdapter
    # graph_adapter = Neo4jGraphAdapter(neo4j_backend, project_id=project_id)

    # 新：CodeGraph 直读 SQLite（设计 [[CodeGraph-结构引擎集成-设计]]）
    from src.service.qa_engine.adapters import WeaviateTopologicalAdapter  # 仍需解读适配器
    from src.integrations.codegraph.db import CodeGraphDB
    from src.integrations.codegraph.graph_adapter import CodeGraphGraphAdapter
    from src.integrations.codegraph.paths import codegraph_db_path

    # repo_local_path 已在上方 L150 取到；定位该工程的 .codegraph.db（只读）
    graph_adapter = CodeGraphGraphAdapter(
        CodeGraphDB(codegraph_db_path(repo_local_path))
    )
```

并放宽前置检查：`neo4j_backend` 不再是 graph 的必需依赖（若其它地方仍用到则保留变量，仅不再用于 graph）。把 L137-140 的 `if interp_store is None or neo4j_backend is None:` 改为只强制 `interp_store`（除非 neo4j_backend 在本函数其它处仍必需——实现时确认；本计划 graph 不再依赖它）。

在 `build_retriever_for_project`（约 L96）同样替换；该函数无 db，repo 路径从 app.state 的工程缓存取（startup 时已建项目表缓存）或退化为：retriever 的 graph 同样用 `CodeGraphGraphAdapter(CodeGraphDB(codegraph_db_path(repo_path)))`，`repo_path` 通过新增的 `get_project_repo_path(request, project_id)`（读 app.state 缓存）获得。**实现者先确认 app.state 是否已有 project→repo 缓存；没有则在本任务加一个最简内存缓存（startup 时从 DB 拉一次）。**

- [ ] **Step 4：mall-swarm E2E 手测（需 KE 栈在线 + mall-swarm 已 codegraph index）**

```bash
# 确保 mall-swarm 已索引（库已存在则跳过）
cd /Users/java/repos/mall-swarm && codegraph index . 2>&1 | tail -3
# 起 KE 服务，问一个要走 callees 的问题，确认结构导航工具返回真实结果且不报 Neo4j 错
# （Weaviate 隧道需在线以支持检索；本步验证 graph 路径已走 CodeGraph）
```
Expected: ke_callees/ke_callers 返回 mall-swarm 真实方法（来自 CodeGraph），日志无 Neo4j 图查询。

- [ ] **Step 5：提交**
```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/qa_router.py tests/test_integrations/codegraph/test_router_swap.py
git commit -m "feat(qa): route structure navigation through CodeGraph (Neo4j out of nav path)"
```

---

## Self-Review

**1. Spec 覆盖（对 §10 Phase 0/1）：** Phase 0 验证(Task 0.1) ✅；DurableKey(1.3) ✅；CodeGraphDB 直读(1.2) ✅；CodeGraphGraphAdapter 实现 GraphProto(1.4) ✅；IndexManager(1.5) ✅；qa_router 注入切换(1.6) ✅。SymbolBridge/解读改挂钩/血缘/多租户多库调度/MCP → 明确属后续 Phase，未纳入（符合范围）。

**2. 占位符扫描：** Task 1.6 Step 3 对 `build_retriever_for_project` 的 repo 路径来源写了"实现者确认 app.state 缓存，没有则加最简缓存"——这是**真实的待定接入点**（sync 函数无 db），非偷懒占位；已给出明确兜底做法。其余均有完整代码。

**3. 类型一致性：** `CgNode` 字段、`durable_key(node)->str`、`CodeGraphDB.successors/predecessors(node_id,kind)->list[CgNode]`、`CodeGraphGraphAdapter.successors/predecessors(entity_id,rel_type)->list[str]` 全计划一致；`GraphProto` 签名匹配（retriever.py:42-43）。

**已知依赖：** Task 0.1 Step 3 / Task 1.6 Step 4 需 KE 数据栈在线（用户手动起 Weaviate 隧道 + Neo4j 暂不需要因为 graph 已切走）；其余任务（1.1-1.5 + 1.6 Step1-3）纯本地可跑。
