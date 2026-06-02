# 代码片段查看器（后端）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) 或 superpowers:executing-plans。Steps 用 checkbox（`- [ ]`）语法。

**Goal:** 给前端加一个 HTTP 端点 `GET /api/projects/{pid}/code-snippet?entity_id=`，按 entity_id 返回代码片段 + 带调用点位置(line/col)的 callees + callers，支撑前端 IDE 式"点击实体看片段、片段内跳转"。

**Architecture:** 复用已有三层——`_resolve`(entity_id→CgNode)、`source_reader.read_snippet`(读片段)、CodeGraph `edges.line/col`(调用点位置)。新增：db 层 `successors_with_locations`、adapter 层 `resolve_first/successors_with_locations/callers`、纯函数 `build_snippet_response`、薄路由 `code_router.py`。仅后端 `knowledge-engineering-auth`，前端另出 plan。

**Tech Stack:** Python · FastAPI · SQLite(只读 .codegraph.db) · pytest。

**设计 spec（已审批）:** `/Users/java/obsidian/01 Engineering/knowledge-engineering/代码片段查看器-设计.md`（API 契约见 §3）

**用户偏好:** Python 中文逐行注释；设计文档 Obsidian 不双写。

**探索已确认的事实（实现照此）:**
- `CgNode`(db.py:18-27)：`id,kind,name,qualified_name,file_path,start_line,end_line,signature`；`_COLS`(db.py:14) 同。`_row_to_node`(db.py:142-157) 按列名映射。`_prefixed('t')`(db.py:133-140) → `t.id,t.kind,...`。
- db.successors(db.py:67-83)：`SELECT {self._prefixed('t')} FROM edges e JOIN nodes t ON e.target=t.id WHERE e.source=? AND e.kind=?`。db.predecessors(db.py:85-101) 对称（返 CgNode 列表，含 .name）。
- 真实 `.codegraph.db` 的 `edges` 表列：`source,target,kind,line,col,metadata,provenance`（line 已实填、col ~90%）。**但测试夹具 `_fixture.py` 的 edges 表只有 `id,source,target,kind,line`——没有 `col`，且 insert 未填 line/col**（本 plan T1 先补）。
- graph_adapter.py：`_resolve`(35-58, split '#'→find_nodes_by_qualified_name→重载用 durable_key 精筛)、`_walk`(60-82, try/except sqlite3.Error→[])、`successors/predecessors`(84-104, 返 durable_key 列表)。已 import `sqlite3`/`Optional`/`_LOG`/`CgNode`/`durable_key`。
- graph_factory.py：`NullGraphAdapter`（已有 successors/predecessors→[]、module_of→None）；`resolve_graph_adapter(repo_local_path)`(缺路径/库→NullGraphAdapter)。
- source_reader.read_snippet(repo_root, file_path, start_line, end_line)→str（source_reader.py:12，1-indexed 闭区间，读不到返 ""）。
- `_path_sandbox.resolve_safe_path(repo_local_path, relative_path)→Path`（tools/_path_sandbox.py:17，越界/绝对路径 raise ValueError）。
- qa_router.py：`router=APIRouter(prefix="/projects/{project_id}/qa", dependencies=[Depends(require_infra_healthy)])`；路由用 `dependencies=[Depends(require_project_role("reporter"))]`；取 repo：`p=await db.get(ProjectModel, project_id); repo_local_path=p.repo_local_path`；`resolve_graph_adapter(repo_local_path)`；`from src.service.permission_deps import require_project_role`、`from src.service.db import get_db`、`from src.service.db_models_homepage import Project as ProjectModel`。
- api.py:78-87：`app.include_router(qa_router)` 等一串挂载（无额外 prefix；外部 `/api` 由 nginx 代理，前端 baseURL `/api`）。

---

## File Structure

| 文件 | 职责 | 动作 |
|---|---|---|
| `tests/test_integrations/codegraph/_fixture.py` | mini db 加 col 列 + line/col 值 | Modify |
| `src/integrations/codegraph/db.py` | `successors_with_locations` | Modify |
| `src/integrations/codegraph/graph_adapter.py` | `resolve_first/successors_with_locations/callers` | Modify |
| `src/integrations/codegraph/graph_factory.py` | `NullGraphAdapter` 同名降级方法 | Modify |
| `src/service/code_router.py` | `_lang_from_path` + `build_snippet_response` + 路由 | Create |
| `src/service/api.py` | 挂载 code_router | Modify |
| `tests/test_integrations/codegraph/test_successors_with_locations.py` | db 单测 | Create |
| `tests/test_integrations/codegraph/test_adapter_snippet_methods.py` | adapter 单测 | Create |
| `tests/test_auth/test_code_snippet_helper.py` | build_snippet_response 单测 | Create |
| `tests/test_auth/test_code_snippet_endpoint.py` | 路由单测 | Create |

外部路径 `/api/projects/{pid}/code-snippet`；FastAPI 内 `code_router` prefix=`/projects/{project_id}`、route=`/code-snippet`（与 qa_router 同款，`/api` 由代理层加）。

---

## Task 1：db.successors_with_locations（+ 扩展测试夹具）

**Files:** Modify `tests/test_integrations/codegraph/_fixture.py`、`src/integrations/codegraph/db.py`；Create `tests/test_integrations/codegraph/test_successors_with_locations.py`

- [ ] **Step 1: 扩展测试夹具 `_fixture.py`（加 col 列 + 填 line/col）**

把 `_fixture.py` 的 edges 建表与插入改为带 col + 位置值。将：
```python
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
            target TEXT NOT NULL, kind TEXT NOT NULL, line INTEGER
        );
```
改为（加 `col INTEGER`）：
```python
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
            target TEXT NOT NULL, kind TEXT NOT NULL, line INTEGER, col INTEGER
        );
```
并把：
```python
    conn.executemany(
        "INSERT INTO edges(source,target,kind) VALUES (?,?,?)",
        [("n1", "n2", "calls"), ("n2", "n3", "calls")]
    )
```
改为（填调用点 line/col；n1 体 40-42 → 调用在 41:8；n2 体 25-30 → 调用在 27:12）：
```python
    # 带调用点位置（line/col）：模拟真实 .codegraph.db 的 edges（CG-2 代码片段查看器用）
    conn.executemany(
        "INSERT INTO edges(source,target,kind,line,col) VALUES (?,?,?,?,?)",
        [("n1", "n2", "calls", 41, 8), ("n2", "n3", "calls", 27, 12)]
    )
```

- [ ] **Step 2: 跑既有 codegraph 测试，确认夹具扩展不破旧测**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python -m pytest tests/test_integrations/codegraph/ -q`
Expected: 全 PASS（加列 + 填值是向后兼容的；既有 successors/predecessors 测试不读 line/col）。若有测试硬断言 edges schema/计数而失败，按其断言语义最小修正（边数仍是 2，不应失败）。

- [ ] **Step 3: 写失败测试**

```python
# tests/test_integrations/codegraph/test_successors_with_locations.py
"""db.successors_with_locations：出边带调用点 line/col、不去重。设计 [[代码片段查看器-设计]] §4。"""
import sqlite3
from src.integrations.codegraph.db import CodeGraphDB
from tests.test_integrations.codegraph._fixture import make_fixture_db


def test_returns_callee_with_line_col(tmp_path):
    db_path = str(tmp_path / "mini.codegraph.db")   # tmp_path 是 pytest 内置临时目录 fixture
    make_fixture_db(db_path)                          # 造迷你库
    db = CodeGraphDB(db_path)
    rows = db.successors_with_locations("n1", "calls")  # n1 --calls(41,8)--> n2
    assert len(rows) == 1
    node, line, col = rows[0]                          # 解包三元组
    assert node.qualified_name == "OmsService::generateOrder"
    assert (line, col) == (41, 8)


def test_leaf_has_no_successors(tmp_path):
    db_path = str(tmp_path / "mini.codegraph.db")
    make_fixture_db(db_path)
    db = CodeGraphDB(db_path)
    assert db.successors_with_locations("n3", "calls") == []   # n3 是叶子，无出边


def test_not_deduped_keeps_each_call_site(tmp_path):
    # 单独造一个库：a 调用 b 两次（不同位置）→ 应返回 2 行（不去重，每个调用点一行）
    db_path = str(tmp_path / "dup.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT, language TEXT, start_line INTEGER, end_line INTEGER, signature TEXT);
        CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, target TEXT,
            kind TEXT, line INTEGER, col INTEGER);
        """
    )
    conn.executemany(
        "INSERT INTO nodes(id,kind,name,qualified_name,file_path,language,start_line,end_line,signature) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [("a", "method", "m", "A::m", "A.java", "java", 1, 20, "void ()"),
         ("b", "method", "h", "B::h", "B.java", "java", 5, 8, "int ()")],
    )
    conn.executemany(
        "INSERT INTO edges(source,target,kind,line,col) VALUES (?,?,?,?,?)",
        [("a", "b", "calls", 3, 4), ("a", "b", "calls", 10, 6)],   # 两次调用同一目标
    )
    conn.commit(); conn.close()
    rows = CodeGraphDB(db_path).successors_with_locations("a", "calls")
    assert len(rows) == 2                                  # 不去重
    assert sorted((l, c) for _, l, c in rows) == [(3, 4), (10, 6)]
```

- [ ] **Step 4: 运行确认失败**

Run: `python -m pytest tests/test_integrations/codegraph/test_successors_with_locations.py -v`
Expected: FAIL（`successors_with_locations` 不存在 → AttributeError）

- [ ] **Step 5: 实现 db.successors_with_locations**

在 `src/integrations/codegraph/db.py` 的 `CodeGraphDB` 里、`predecessors` 方法之后加：
```python
    def successors_with_locations(
        self, node_id: str, kind: str = "calls"
    ) -> list[tuple[CgNode, int, Optional[int]]]:
        """出边目标(callees) + 每次调用的调用点位置 (line, col)。

        与 successors 的区别：① 多带 edges.line/col；② **不去重**——同一目标被调用多次
        会返回多行（每个调用点一行），供前端在代码片段里逐个标成可点击跳转。
        col 在真实库里可能为 NULL（约 10%）→ 返回 None。

        Args:
            node_id: 起点（caller）节点 ID
            kind:    边类型，默认 'calls'
        Returns:
            [(callee CgNode, line, col), ...]，按 (line, col) 升序
        """
        with self._connect() as conn:
            # SELECT t.* 投影目标节点列（_row_to_node 按列名读）+ edges 的 line/col（别名避免与 nodes 列混）
            rows = conn.execute(
                f"SELECT {self._prefixed('t')}, e.line AS edge_line, e.col AS edge_col "
                "FROM edges e JOIN nodes t ON e.target = t.id "
                "WHERE e.source = ? AND e.kind = ? "
                "ORDER BY e.line, e.col",   # 稳定顺序：按调用点位置排
                (node_id, kind),
            ).fetchall()
        # 列表推导式：每行 → (CgNode, line, col) 三元组；col 可能为 None
        return [(self._row_to_node(r), r["edge_line"], r["edge_col"]) for r in rows]
```

- [ ] **Step 6: 运行确认通过 + 回归**

Run: `python -m pytest tests/test_integrations/codegraph/ -q`
Expected: 新测 PASS + 既有 codegraph 测试全绿

- [ ] **Step 7: 提交**

```bash
git add tests/test_integrations/codegraph/_fixture.py src/integrations/codegraph/db.py tests/test_integrations/codegraph/test_successors_with_locations.py
git commit -m "feat(codegraph): db.successors_with_locations (callees with call-site line/col)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2：adapter 层 resolve_first / successors_with_locations / callers

**Files:** Modify `src/integrations/codegraph/graph_adapter.py`、`src/integrations/codegraph/graph_factory.py`；Create `tests/test_integrations/codegraph/test_adapter_snippet_methods.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_integrations/codegraph/test_adapter_snippet_methods.py
"""CodeGraphGraphAdapter 的代码片段三方法 + NullGraphAdapter 降级。设计 [[代码片段查看器-设计]] §4。"""
from src.integrations.codegraph.db import CodeGraphDB
from src.integrations.codegraph.graph_adapter import CodeGraphGraphAdapter
from src.integrations.codegraph.graph_factory import NullGraphAdapter
from tests.test_integrations.codegraph._fixture import make_fixture_db


def _adapter(tmp_path):
    db_path = str(tmp_path / "mini.codegraph.db")
    make_fixture_db(db_path)
    return CodeGraphGraphAdapter(CodeGraphDB(db_path))


def test_resolve_first_returns_node(tmp_path):
    adp = _adapter(tmp_path)
    node = adp.resolve_first("OmsCtrl::generateOrder")     # 无 #params → qn 唯一命中 n1
    assert node is not None and node.qualified_name == "OmsCtrl::generateOrder"


def test_resolve_first_none_for_unknown(tmp_path):
    assert _adapter(tmp_path).resolve_first("Ghost::x#()") is None


def test_successors_with_locations(tmp_path):
    adp = _adapter(tmp_path)
    callees = adp.successors_with_locations("OmsCtrl::generateOrder")  # n1 -> n2 (41,8)
    assert callees == [
        {"entity_id": "OmsService::generateOrder#(OrderParam)", "name": "generateOrder", "line": 41, "col": 8}
    ]


def test_callers(tmp_path):
    adp = _adapter(tmp_path)
    callers = adp.callers("OmsService::generateOrder")     # n2 被 n1 调用
    assert callers == [{"entity_id": "OmsCtrl::generateOrder#(OrderParam)", "name": "generateOrder"}]


def test_null_adapter_degrades(tmp_path):
    null = NullGraphAdapter()
    assert null.resolve_first("X::y#()") is None
    assert null.successors_with_locations("X::y#()") == []
    assert null.callers("X::y#()") == []
```

> 说明：durable_key 由 qualified_name + 参数签名拼成，故 n1/n2 的 entity_id 末尾带 `#(OrderParam)`（来自夹具 signature `"... (OrderParam)"`）。

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_integrations/codegraph/test_adapter_snippet_methods.py -v`
Expected: FAIL（方法不存在）

- [ ] **Step 3: 实现 CodeGraphGraphAdapter 三方法**

在 `src/integrations/codegraph/graph_adapter.py` 的 `CodeGraphGraphAdapter` 里、`predecessors` 之后加：
```python
    def resolve_first(self, entity_id: str) -> Optional[CgNode]:
        """把 entity_id 解析为单个 CgNode（重载/多命中取首个）；无命中或 sqlite 异常 → None。"""
        try:
            nodes = self._resolve(entity_id)          # 复用解析（split '#' → 查 qualified_name）
            return nodes[0] if nodes else None         # 取首个；同 qn 重载在同文件/同模块
        except sqlite3.Error as e:
            _LOG.warning("[codegraph] resolve_first 失败，返回 None (entity_id=%s): %s", entity_id, e)
            return None

    def successors_with_locations(self, entity_id: str) -> list[dict]:
        """entity_id 的 callees + 调用点位置：[{entity_id, name, line, col}, ...]。

        不去重（每个调用点一项），供前端在片段里逐个标可点击。sqlite 异常 → []（降级）。
        """
        try:
            nodes = self._resolve(entity_id)
            if not nodes:
                return []
            out: list[dict] = []
            # 只取首个节点的出边（与 resolve_first 选的片段一致，保证调用点落在该片段内）
            for tgt, line, col in self._db.successors_with_locations(nodes[0].id, "calls"):
                out.append({"entity_id": durable_key(tgt), "name": tgt.name, "line": line, "col": col})
            return out
        except sqlite3.Error as e:
            _LOG.warning("[codegraph] successors_with_locations 失败，返回 [] (entity_id=%s): %s", entity_id, e)
            return []

    def callers(self, entity_id: str) -> list[dict]:
        """谁调用了 entity_id：[{entity_id, name}, ...]（按 entity_id 去重）。sqlite 异常 → []。"""
        try:
            nodes = self._resolve(entity_id)
            if not nodes:
                return []
            out: list[dict] = []
            seen: set[str] = set()                     # 反向导航是列表，按 caller 去重
            for src in self._db.predecessors(nodes[0].id, "calls"):
                key = durable_key(src)
                if key not in seen:
                    seen.add(key)
                    out.append({"entity_id": key, "name": src.name})
            return out
        except sqlite3.Error as e:
            _LOG.warning("[codegraph] callers 失败，返回 [] (entity_id=%s): %s", entity_id, e)
            return []
```

- [ ] **Step 4: 实现 NullGraphAdapter 三方法**

在 `src/integrations/codegraph/graph_factory.py` 的 `NullGraphAdapter` 里加（与现有 module_of 降级风格一致）：
```python
    def resolve_first(self, entity_id: str) -> Optional[CgNode]:
        """无 CodeGraph 索引 → 解析不出节点。"""
        return None

    def successors_with_locations(self, entity_id: str) -> list[dict]:
        """无 CodeGraph 索引 → 无调用点。"""
        return []

    def callers(self, entity_id: str) -> list[dict]:
        """无 CodeGraph 索引 → 无调用者。"""
        return []
```
> 注意 import：graph_factory.py 已 import `Optional`；`CgNode` 若未 import，在文件顶部加 `from src.integrations.codegraph.db import CgNode`（仅类型注解用）。先 Read 确认。

- [ ] **Step 5: 运行确认通过 + 回归**

Run: `python -m pytest tests/test_integrations/codegraph/ -q`
Expected: 全 PASS

- [ ] **Step 6: 提交**

```bash
git add src/integrations/codegraph/graph_adapter.py src/integrations/codegraph/graph_factory.py tests/test_integrations/codegraph/test_adapter_snippet_methods.py
git commit -m "feat(codegraph): adapter resolve_first/successors_with_locations/callers (+NullGraphAdapter)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3：build_snippet_response 纯函数 + 语言判定

**Files:** Create `src/service/code_router.py`（先只放 helper）、`tests/test_auth/test_code_snippet_helper.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_auth/test_code_snippet_helper.py
"""build_snippet_response：组装 entity_id → 片段 JSON 的纯逻辑（无 HTTP/鉴权）。设计 [[代码片段查看器-设计]] §3。"""
from src.service.code_router import build_snippet_response, _lang_from_path


class _Node:
    qualified_name = "OmsPortalOrderController::confirmReceiveOrder"
    kind = "method"
    file_path = "mall-portal/src/.../OmsPortalOrderController.java"
    start_line = 1
    end_line = 2


class _Adapter:
    """假图适配器：resolve_first/successors_with_locations/callers。"""
    def __init__(self, node):
        self._node = node
    def resolve_first(self, entity_id):
        return self._node
    def successors_with_locations(self, entity_id):
        return [{"entity_id": "OmsPortalOrderService::confirmReceiveOrder#(Long)", "name": "confirmReceiveOrder", "line": 2, "col": 8}]
    def callers(self, entity_id):
        return [{"entity_id": "X::y#()", "name": "y"}]


def test_lang_from_path():
    assert _lang_from_path("a/B.java") == "java"
    assert _lang_from_path("a/M.xml") == "xml"
    assert _lang_from_path("a/s.py") == "python"
    assert _lang_from_path("a/app.yml") == "yaml"
    assert _lang_from_path("a/x.unknownext") == "plaintext"


def test_build_ok(tmp_path):
    # 造真实源码文件（read_snippet 会去读）
    f = tmp_path / "mall-portal" / "src"
    f.mkdir(parents=True)
    (f / "OmsPortalOrderController.java").write_text("line1\nline2\n", encoding="utf-8")
    node = _Node()
    node.file_path = "mall-portal/src/OmsPortalOrderController.java"   # 相对 repo 根
    out = build_snippet_response(_Adapter(node), str(tmp_path), "OmsPortalOrderController::confirmReceiveOrder#(Long)")
    assert out["language"] == "java"
    assert out["code"] == "line1\nline2"          # read_snippet 读 1-2 行、去尾换行
    assert out["start_line"] == 1 and out["end_line"] == 2
    assert out["qualified_name"] == "OmsPortalOrderController::confirmReceiveOrder"
    assert out["callees"][0]["line"] == 2 and out["callees"][0]["col"] == 8
    assert out["callers"] == [{"entity_id": "X::y#()", "name": "y"}]


def test_build_none_when_entity_missing(tmp_path):
    class _NullAdp:
        def resolve_first(self, entity_id): return None
        def successors_with_locations(self, entity_id): return []
        def callers(self, entity_id): return []
    assert build_snippet_response(_NullAdp(), str(tmp_path), "Ghost::x#()") is None


def test_build_none_when_path_escapes(tmp_path):
    class _EscapeNode(_Node):
        file_path = "../../etc/passwd"            # 越界
    assert build_snippet_response(_Adapter(_EscapeNode()), str(tmp_path), "X::y#()") is None
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_auth/test_code_snippet_helper.py -v`
Expected: FAIL（模块/函数不存在 → ImportError）

- [ ] **Step 3: 创建 `src/service/code_router.py`（helper 部分）**

```python
# src/service/code_router.py
"""代码片段查看端点：按 entity_id 返回源码片段 + 带调用点位置的 callees + callers。

支撑前端 IDE 式"点击实体看片段、片段内跳转"（设计 [[代码片段查看器-设计]]）。
独立 router：与 QA 聊天分离，是纯代码源访问，不依赖 Weaviate/LLM 基础设施。
"""
from __future__ import annotations  # PEP 563：类型注解可前向引用

import os  # 标准库：取文件后缀判语言

from fastapi import APIRouter, Depends, HTTPException, Query  # FastAPI 路由/依赖/异常/查询参数
from sqlalchemy.ext.asyncio import AsyncSession  # 异步 DB 会话类型

from src.integrations.codegraph.source_reader import read_snippet  # 按行号读源码片段
from src.service.qa_engine.tools._path_sandbox import resolve_safe_path  # 路径沙箱（防越界）

# 文件后缀 → 前端 Monaco/语法高亮用的语言名；缺省 plaintext
_LANG_BY_SUFFIX = {
    ".java": "java", ".xml": "xml", ".py": "python",
    ".yml": "yaml", ".yaml": "yaml", ".sql": "sql",
    ".js": "javascript", ".ts": "typescript", ".json": "json",
    ".kt": "kotlin", ".go": "go", ".properties": "ini",
}


def _lang_from_path(file_path: str) -> str:
    """按文件后缀返回语言名（小写匹配）；未知后缀返回 'plaintext'。"""
    # os.path.splitext('a/B.java') → ('a/B', '.java')；取 [1] 后缀并转小写
    ext = os.path.splitext(file_path)[1].lower()
    # dict.get(key, default)：键不存在时返回默认值，避免 KeyError
    return _LANG_BY_SUFFIX.get(ext, "plaintext")


def build_snippet_response(graph_adapter, repo_local_path: str, entity_id: str) -> dict | None:
    """组装 entity_id → 代码片段 JSON（纯逻辑，无 HTTP/鉴权，便于单测）。

    返回 None 表示"该实体无可用源码"（entity 未解析出 / file_path 越界）→ 调用方映射 404。

    Args:
        graph_adapter: CodeGraphGraphAdapter 或 NullGraphAdapter（有 resolve_first/successors_with_locations/callers）
        repo_local_path: 工程源码根目录
        entity_id: 实体持久 key（qualified_name#params）
    Returns:
        spec §3 的 dict，或 None
    """
    # 1. entity_id → CgNode（无 → None → 调用方 404）
    node = graph_adapter.resolve_first(entity_id)
    if node is None:
        return None
    # 2. 防御性沙箱：file_path 来自 CodeGraph（受信），但仍校验不越界
    #    （防异常 .codegraph.db 里出现绝对路径 / '..' 导致 read_snippet 读到仓库外）
    try:
        resolve_safe_path(repo_local_path, node.file_path)
    except ValueError:
        return None
    # 3. 读片段（1-indexed 闭区间；读不到返 ""，前端显示空片段而非崩）
    code = read_snippet(repo_local_path, node.file_path, node.start_line, node.end_line)
    # 4. 组装 spec §3 JSON
    return {
        "entity_id": entity_id,
        "qualified_name": node.qualified_name,
        "kind": node.kind,
        "file_path": node.file_path,
        "language": _lang_from_path(node.file_path),
        "start_line": node.start_line,
        "end_line": node.end_line,
        "code": code,
        "callees": graph_adapter.successors_with_locations(entity_id),  # 带 line/col 的调用点
        "callers": graph_adapter.callers(entity_id),                    # 反向导航列表
    }
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_auth/test_code_snippet_helper.py -v`
Expected: PASS（5 测试）

- [ ] **Step 5: 提交**

```bash
git add src/service/code_router.py tests/test_auth/test_code_snippet_helper.py
git commit -m "feat(qa): build_snippet_response helper (entity_id -> snippet JSON)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4：FastAPI 路由 + 挂载

**Files:** Modify `src/service/code_router.py`（加 router）、`src/service/api.py`（挂载）；Create `tests/test_auth/test_code_snippet_endpoint.py`

- [ ] **Step 1: 写失败测试**

先 Read 一个现有的 `tests/test_auth/` 端点测试（如测 `/qa/sessions` 或 project 相关的），学清本仓鉴权 fixture（已认证 TestClient + 在测试 DB 种子化 Project 与 project 访问权限）的用法。然后照该 fixture 写：
```python
# tests/test_auth/test_code_snippet_endpoint.py
"""GET /projects/{pid}/code-snippet 路由：happy/404/无源码路径。设计 [[代码片段查看器-设计]] §3。
鉴权与 DB 种子沿用本仓既有 test_auth 端点测试 fixture（已认证 client + 有 reporter 权限的 project）。"""
# —— 按既有 test_auth conftest 提供的 fixture 名替换以下占位 ——
# 关键断言（happy path，假设测试已 seed 一个带 repo_local_path 的 project 且 .codegraph.db 含某 entity）：
#   resp = client.get(f"/projects/{pid}/code-snippet", params={"entity_id": known_entity_id})
#   assert resp.status_code == 200
#   body = resp.json()
#   assert set(body) >= {"entity_id","qualified_name","kind","file_path","language","start_line","end_line","code","callees","callers"}
# 未知实体 → 404：
#   resp = client.get(f"/projects/{pid}/code-snippet", params={"entity_id": "Ghost::x#()"})
#   assert resp.status_code == 404
# project 无 repo_local_path → 404：
#   （seed 一个 repo_local_path=None 的 project）assert 404
```
> 实现者：用 monkeypatch 把 `src.service.code_router.resolve_graph_adapter` 替换为返回一个假 adapter（resolve_first/successors_with_locations/callers），并把 `build_snippet_response` 的 read_snippet 指向 tmp 源码，可绕开真实 .codegraph.db 与磁盘依赖；或 seed 一个真实 mini .codegraph.db + 源码文件。鉴权务必复用既有 fixture，勿绕过 `require_project_role`（要覆盖 403 路径）。

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_auth/test_code_snippet_endpoint.py -v`
Expected: FAIL（路由不存在 → 404 但非预期/或 ImportError）

- [ ] **Step 3: 在 code_router.py 加 router + 路由**

在 `src/service/code_router.py` 末尾追加（import 处补 `require_project_role`/`get_db`/`ProjectModel`/`resolve_graph_adapter`）：
```python
from src.service.db import get_db                                   # 异步 DB 会话依赖
from src.service.db_models_homepage import Project as ProjectModel  # Project ORM
from src.service.permission_deps import require_project_role        # 工程角色鉴权工厂
from src.integrations.codegraph.graph_factory import resolve_graph_adapter  # repo→图适配器

# router：prefix 与 qa_router 同款（外部 /api 由代理层加）；不挂 require_infra_healthy
# —— 本端点只读 CodeGraph + 源码文件，不依赖 Weaviate/LLM，infra 不健康时也应可用
router = APIRouter(prefix="/projects/{project_id}", tags=["code"])


@router.get(
    "/code-snippet",
    # 鉴权：工程成员（reporter+）才能看源码（与 /qa 同一最低门槛）
    dependencies=[Depends(require_project_role("reporter"))],
)
async def get_code_snippet(
    project_id: str,
    entity_id: str = Query(..., min_length=1, description="实体持久 key，如 OmsXxx::m#(Long)"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """按 entity_id 返回代码片段 + callees(带调用点 line/col) + callers。

    404：工程不存在 / 工程未配源码路径 / 该实体无可用源码。
    """
    # 1. 工程存在 + 取源码根
    p = await db.get(ProjectModel, project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="工程不存在")
    if not p.repo_local_path:
        raise HTTPException(status_code=404, detail="该工程未配置源码路径")
    # 2. 解析图适配器（缺 .codegraph.db → NullGraphAdapter → 下面 resolve_first 返 None → 404）
    graph_adapter = resolve_graph_adapter(p.repo_local_path)
    # 3. 组装（纯逻辑）
    result = build_snippet_response(graph_adapter, p.repo_local_path, entity_id)
    if result is None:
        raise HTTPException(status_code=404, detail="未找到该实体的源码")
    return result
```

- [ ] **Step 4: 在 api.py 挂载**

`src/service/api.py`：在 `from src.service.qa_router import router as qa_router`（L26 附近）后加 import，并在 `app.include_router(qa_router)`（L81 附近）后加挂载：
```python
from src.service.code_router import router as code_router   # 代码片段查看端点
```
```python
app.include_router(code_router)   # GET /projects/{pid}/code-snippet（代码片段查看器后端）
```

- [ ] **Step 5: 运行确认通过 + 全量回归**

Run: `python -m pytest tests/test_auth/test_code_snippet_endpoint.py -v && python -m pytest tests/ -q 2>&1 | tail -5`
Expected: 新测 PASS；全量 0 failed（基线之前 718+，新增本特性测试后更多）

- [ ] **Step 6: 提交**

```bash
git add src/service/code_router.py src/service/api.py tests/test_auth/test_code_snippet_endpoint.py
git commit -m "feat(qa): GET /projects/{pid}/code-snippet endpoint (mount code_router)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5：部署 + 服务器侧 E2E（⚠️ 需用户授权部署）

> 前置：用户授权部署到蓝队云。本任务不自动部署。

- [ ] **Step 1: 推送** `cd /Users/java/knowledge-engineering-auth && git push origin release-0513`
- [ ] **Step 2:（授权后）服务器拉取 + 重启** `ssh -p 26666 root@103.47.81.50 'cd /opt/knowledge-engineering && git -c safe.directory=/opt/knowledge-engineering pull --ff-only origin release-0513 && systemctl restart ke-api && sleep 4 && systemctl is-active ke-api'`（github TLS 偶发抖动 → 重试几次）
- [ ] **Step 3: 服务器侧 E2E**（curl 端点，带一个登录用户 JWT；或 mall-swarm 真实 entity_id）：
  - `GET /api/projects/mall-swarm/code-snippet?entity_id=OmsPortalOrderController::confirmReceiveOrder#(Long)`
  - 期望：200，返回 `code` 非空、`language=java`、`callees` 含若干 `{entity_id,name,line,col}`（line 落在 start_line~end_line 内）、`callers` 列表。
  - 未知 entity → 404。
- [ ] **Step 4: 回填 Obsidian §12** —— `代码片段查看器-设计.md` §12 记后端 commit、端点 E2E 结果、部署 commit；标注"后端完成，前端 plan 待出"。不双写仓库。

---

## Self-Review

**1. Spec 覆盖（§3-§8 后端部分）：** API 契约 §3 → T3/T4 端点返回结构逐字段对齐 ✅；§4 successors_with_locations(db+adapter) → T1/T2 ✅；§4 新 code_router + api.py 挂载 → T4 ✅；§4 复用 _resolve/source_reader/沙箱/鉴权 → T3/T4 ✅；§5 callees 不去重 → T1 test_not_deduped + T2 ✅；§7 降级（entity 404 / 越界 / NullGraphAdapter）→ T2 null + T3 none/escape + T4 404 ✅；§8 测试（db/adapter/端点）→ T1/T2/T3/T4 ✅；§9 Phase1 仅方法调用精确跳转 → callees=successors_with_locations(calls) ✅（类名跳转未做，符合分期）。前端（§5 前端组件）不在本 plan（另出）。

**2. 占位符扫描：** T1-T3 完整 before/after + 完整测试代码；T4 路由代码完整，路由测试因依赖既有 test_auth 鉴权 fixture，给了明确断言 + "先读既有端点测试照搬 fixture"的具体指引（非 TBD）；T5 给了具体 entity_id + 期望。无 TODO/TBD。

**3. 类型一致性：** `successors_with_locations` —— db 层签名 `(node_id,kind)->list[tuple[CgNode,int,Optional[int]]]`、adapter 层 `(entity_id)->list[dict{entity_id,name,line,col}]`，两层职责不同名同（db 给元组、adapter 给 dict），T2 调 `self._db.successors_with_locations(nodes[0].id,"calls")` 解包 `(tgt,line,col)` 与 db 返回一致 ✅；`resolve_first/callers` 在 adapter 与 NullGraphAdapter 签名一致 ✅；`build_snippet_response(graph_adapter,repo_local_path,entity_id)` 在 T3 定义、T4 调用一致 ✅；返回 dict 键与 spec §3 + T3 测试 + T4 断言三处一致 ✅。
