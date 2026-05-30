# CodeGraph Phase 2a — 重灌 CodeEntity（按 qualified_name）+ 增量更新 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 从 CodeGraph 的 `.codegraph.db` 读方法节点 + 读源码片段 → DashScope embed → 写 Weaviate CodeEntity，**按 `durable_key`(qualified_name#签名) 落库**（取代 canonical_v1）；并支持代码更新后的**增量更新**（只重 embed 改动方法 + 删孤儿）。

**Architecture:** 复用现有原语 `get_embeddings_batch`(DashScope) + `WeaviateVectorStore.add`(tenant) + Phase 1 的 `CodeGraphDB`/`durable_key`。新写一个**独立重灌例程**（不动 `graph.build_from` god method）：CodeGraph 方法节点 →(durable_key, name, 源码片段)→ embed → store。增量靠**内容 hash checkpoint**（现有 `EmbeddingCheckpoint` 是 id-only，会漏掉"改了方法体、key 不变"的情况，故 2a.2 另建内容感知 checkpoint）。

**Tech Stack:** Python · 标准库 sqlite3/hashlib · DashScope(get_embeddings_batch) · Weaviate(WeaviateVectorStore) · pytest。

**设计 spec（已审批）:** `/Users/java/obsidian/01 Engineering/knowledge-engineering/CodeGraph-结构引擎集成-设计.md`（§10 Phase 2 / §5）。本计划只覆盖 **2a**（CodeEntity 重灌 + 增量），不含 2b 解读重生 / 2c 检索器接通。

**用户偏好:** Python 代码**中文逐行注释**（学习者）；下方代码块已带注释，保留。

**关键事实（实现照此）:**
- CodeGraph `nodes` 表：`kind='method'` 的行有 `qualified_name / name / file_path / start_line / end_line / signature`，**但无方法体**——源码片段要按 `<repo>/<file_path>` 的 `[start_line:end_line]` 自己读。
- Phase 1 已有：`src/integrations/codegraph/db.py`(`CodeGraphDB`, `CgNode`)、`durable_key.py`(`durable_key(node)->str`)、`paths.py`(`codegraph_db_path`)。
- `WeaviateVectorStore.add(entity_id, vector, entity_type=None, name=None, code_snippet=None, *, tenant=None)`；`get_embeddings_batch(texts: list[str]) -> list[list[float]]`。
- 多租户：tenant = project_id；每项目一个 `.codegraph.db`（在 `<repo_local_path>/.codegraph/codegraph.db`）。

**⚠️ 操作依赖（E2E 任务需用户先起栈）:** Weaviate(:8080 隧道) + DashScope key + mall-swarm 已 `codegraph index`。纯代码任务（2a.1/2a.2/2a.5 的单测）用假 embedder/假 store，无需栈。

---

## File Structure

| 文件 | 职责 | 动作 |
|---|---|---|
| `src/integrations/codegraph/db.py` | 加 `iter_method_nodes()`（列全部 method 节点） | Modify |
| `src/integrations/codegraph/source_reader.py` | 按 file_path+行号从仓库读源码片段 | Create |
| `src/integrations/codegraph/repopulate.py` | 重灌编排：节点→(key,name,snippet)→embed→store | Create |
| `src/integrations/codegraph/content_checkpoint.py` | 内容感知 checkpoint（{key: code_hash}），增量用 | Create |
| `src/integrations/codegraph/cli_repopulate.py` | CLI 入口：从 project.yaml 取配置跑重灌 | Create |
| `tests/test_integrations/codegraph/test_repopulate*.py` 等 | 单测（假 embedder/store） | Create |

---

## Task 2a.1：CodeGraphDB.iter_method_nodes + source_reader

**Files:** Modify `src/integrations/codegraph/db.py`；Create `src/integrations/codegraph/source_reader.py`；Test `tests/test_integrations/codegraph/test_repopulate_inputs.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_integrations/codegraph/test_repopulate_inputs.py
"""重灌输入单测：列方法节点 + 按行号读源码片段。"""
from tests.test_integrations.codegraph._fixture import make_fixture_db
from src.integrations.codegraph.db import CodeGraphDB
from src.integrations.codegraph.source_reader import read_snippet


def test_iter_method_nodes(tmp_path):
    db_path = str(tmp_path / "codegraph.db")
    make_fixture_db(db_path)                       # 夹具里 3 个 method 节点
    db = CodeGraphDB(db_path)
    qns = sorted(n.qualified_name for n in db.iter_method_nodes())
    assert qns == ["OmsCtrl::generateOrder", "OmsOrderDao::save", "OmsService::generateOrder"]


def test_read_snippet(tmp_path):
    # 造一个源码文件，验证按 1-indexed 行号 [start,end] 切片
    f = tmp_path / "Foo.java"
    f.write_text("L1\nL2\nL3\nL4\nL5\n", encoding="utf-8")
    snippet = read_snippet(str(tmp_path), "Foo.java", 2, 4)
    assert snippet == "L2\nL3\nL4"


def test_read_snippet_missing_file_returns_empty(tmp_path):
    # 文件不存在 → 返回空串（不抛），重灌时跳过
    assert read_snippet(str(tmp_path), "nope.java", 1, 3) == ""
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && python -m pytest tests/test_integrations/codegraph/test_repopulate_inputs.py -v`
Expected: FAIL（`iter_method_nodes` / `read_snippet` 不存在）

- [ ] **Step 3: 实现**

在 `src/integrations/codegraph/db.py` 的 `CodeGraphDB` 类里加方法（复用已有 `_COLS` / `_row_to_node`）：

```python
    def iter_method_nodes(self) -> list[CgNode]:
        """列出所有 kind='method' 的节点（重灌 CodeEntity 用）。"""
        # 一次性查全部方法节点；mall-swarm ~1.5万，全量读进内存可接受
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_COLS} FROM nodes WHERE kind = 'method'"
            ).fetchall()
        return [self._row_to_node(r) for r in rows]
```

新建 `src/integrations/codegraph/source_reader.py`：

```python
# src/integrations/codegraph/source_reader.py
"""按 CodeGraph 节点的 file_path + 行号，从仓库源码里切出方法片段。

CodeGraph 的 nodes 表不存方法体，只有 file_path/start_line/end_line，
所以代码片段要回源码文件读。行号是 1-indexed、闭区间 [start, end]。
"""
from __future__ import annotations

import os


def read_snippet(repo_root: str, file_path: str, start_line: int, end_line: int) -> str:
    """读 <repo_root>/<file_path> 的第 start_line~end_line 行（1-indexed，含两端）。

    文件读不到 / 行号非法 → 返回空串（调用方据此跳过，不让单个文件失败拖垮整轮）。
    """
    full = os.path.join(repo_root, file_path)       # 拼绝对路径
    try:
        # errors="replace"：遇到非法编码用占位符替代，不抛异常
        with open(full, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()                  # 按行读成列表（每行带换行符）
    except OSError:
        return ""                                   # 文件不存在/不可读 → 空
    if start_line < 1 or end_line < start_line:     # 行号非法 → 空
        return ""
    # 列表切片是 0-indexed、右开；1-indexed 闭区间 [start,end] → [start-1:end]
    chunk = lines[start_line - 1:end_line]
    return "".join(chunk).rstrip("\n")              # 拼回字符串，去掉结尾多余换行
```

- [ ] **Step 4: 运行确认通过**（3 passed）
- [ ] **Step 5: 提交**
```bash
git add src/integrations/codegraph/db.py src/integrations/codegraph/source_reader.py tests/test_integrations/codegraph/test_repopulate_inputs.py
git commit -m "feat(codegraph): iter_method_nodes + source snippet reader (repopulate inputs)"
```

---

## Task 2a.2：重灌编排器（全量）

**Files:** Create `src/integrations/codegraph/repopulate.py`；Test `tests/test_integrations/codegraph/test_repopulate.py`

> 用**假 embedder + 假 store**单测，不碰 DashScope/Weaviate。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_integrations/codegraph/test_repopulate.py
"""重灌编排单测：节点→(key,name,snippet)→embed→store；用假依赖。"""
from tests.test_integrations.codegraph._fixture import make_fixture_db
from src.integrations.codegraph.db import CodeGraphDB
from src.integrations.codegraph.repopulate import repopulate_code_entities


class _FakeEmbedder:
    """假 embedder：每条文本返回一个固定维度的假向量，记录被 embed 的文本。"""
    def __init__(self):
        self.embedded = []
    def __call__(self, texts):
        self.embedded.extend(texts)
        return [[0.1, 0.2, 0.3] for _ in texts]     # 维度无所谓，假 store 不校验


class _FakeStore:
    """假向量库：记录 add 调用的 (entity_id, name, code_snippet)。"""
    def __init__(self):
        self.added = []
    def add(self, entity_id, vector, entity_type=None, name=None, code_snippet=None, *, tenant=None):
        self.added.append({"id": entity_id, "name": name, "snippet": code_snippet, "tenant": tenant})


def test_repopulate_keys_by_durable_key(tmp_path):
    db_path = str(tmp_path / "codegraph.db")
    make_fixture_db(db_path)
    # 夹具节点的 file_path 是 'Ctrl.java' 等（不存在）→ 源码读不到返空，仍按节点 signature 生成 key
    # 为让片段非空，给 repo_root 造对应文件
    for fn, body in [("Ctrl.java", "ctrl-body\n"), ("Svc.java", "svc-body\n"), ("Dao.java", "dao-body\n")]:
        (tmp_path / fn).write_text("x\n" * 50, encoding="utf-8")

    embedder, store = _FakeEmbedder(), _FakeStore()
    stats = repopulate_code_entities(
        db=CodeGraphDB(db_path),
        repo_root=str(tmp_path),
        project_id="mall-swarm",
        embed_batch=embedder,
        store=store,
    )
    ids = sorted(a["id"] for a in store.added)
    # 落库 key 是 durable_key（qualified_name#参数签名），不是 canonical_v1
    assert "OmsService::generateOrder#(OrderParam)" in ids
    assert "OmsOrderDao::save#(OmsOrder)" in ids
    # 每条都带 tenant=project_id
    assert all(a["tenant"] == "mall-swarm" for a in store.added)
    assert stats["embedded"] == 3
```

- [ ] **Step 2: 运行确认失败** → FAIL（模块不存在）

- [ ] **Step 3: 实现**

```python
# src/integrations/codegraph/repopulate.py
"""把 CodeGraph 方法节点重灌进 Weaviate CodeEntity，按 durable_key 落库。

独立例程：不动 graph.build_from。依赖以「可注入」方式传入（embed_batch / store），
便于单测用假实现，也便于将来换 embedder/store。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from src.integrations.codegraph.db import CodeGraphDB
from src.integrations.codegraph.durable_key import durable_key
from src.integrations.codegraph.source_reader import read_snippet

_LOG = logging.getLogger(__name__)

# embed_batch 的类型：给一批文本、返回一批向量
EmbedBatch = Callable[[list[str]], list[list[float]]]


def repopulate_code_entities(
    *,
    db: CodeGraphDB,
    repo_root: str,
    project_id: str,
    embed_batch: EmbedBatch,
    store: Any,
    batch_size: int = 10,
    skip_keys: Optional[set[str]] = None,
) -> dict:
    """全量重灌：遍历方法节点 → durable_key + 源码片段 → embed → store.add。

    :param db: Phase 1 的只读 CodeGraphDB
    :param repo_root: 工程源码根（读片段用），通常 = repo_local_path
    :param project_id: 多租户 tenant
    :param embed_batch: 批量 embed 函数（真用 get_embeddings_batch，测试用假的）
    :param store: 向量库（需有 add(entity_id, vector, entity_type, name, code_snippet, *, tenant)）
    :param batch_size: 每批 embed 多少条（DashScope 上限 10）
    :param skip_keys: 已完成的 durable_key 集合（断点续跑/增量时传，跳过它们）
    :returns: 统计 {scanned, embedded, skipped}
    """
    skip_keys = skip_keys or set()                  # None → 空集合
    # 1) 收集待处理三元组 (durable_key, name, snippet)
    pending: list[tuple[str, str, str]] = []
    scanned = 0
    for node in db.iter_method_nodes():
        scanned += 1
        key = durable_key(node)                     # qualified_name#签名
        if key in skip_keys:                        # 增量/续跑：已完成则跳过
            continue
        snippet = read_snippet(repo_root, node.file_path, node.start_line, node.end_line)
        if not snippet:                             # 读不到源码 → 跳过（无法 embed）
            continue
        pending.append((key, node.name, snippet))

    # 2) 分批 embed + 写库
    embedded = 0
    for i in range(0, len(pending), batch_size):    # 按 batch_size 切片
        chunk = pending[i:i + batch_size]
        vecs = embed_batch([snip for _, _, snip in chunk])   # 一批文本 → 一批向量
        # zip 同步迭代三元组与向量，逐条写库
        for (key, name, snip), vec in zip(chunk, vecs):
            store.add(
                key, vec,
                entity_type="method",
                name=name,
                code_snippet=snip,
                tenant=project_id,                  # 多租户隔离
            )
            embedded += 1

    stats = {"scanned": scanned, "embedded": embedded, "skipped": scanned - embedded}
    _LOG.info("[codegraph] CodeEntity 重灌：扫描 %d，embed %d", scanned, embedded)
    return stats
```

- [ ] **Step 4: 运行确认通过**（1 passed）+ 全量 `python -m pytest tests/test_integrations/ -q`
- [ ] **Step 5: 提交**
```bash
git add src/integrations/codegraph/repopulate.py tests/test_integrations/codegraph/test_repopulate.py
git commit -m "feat(codegraph): full CodeEntity repopulate orchestrator (keyed by durable_key)"
```

---

## Task 2a.3：CLI 入口 + 接 DashScope/Weaviate 真依赖

**Files:** Create `src/integrations/codegraph/cli_repopulate.py`；Test `tests/test_integrations/codegraph/test_cli_repopulate.py`

- [ ] **Step 1: 写失败测试**（只测"配置→依赖装配"，不真跑 embed）

```python
# tests/test_integrations/codegraph/test_cli_repopulate.py
"""CLI 装配单测：从 project dict 解析出 repo_root / project_id / weaviate 配置。"""
from src.integrations.codegraph.cli_repopulate import resolve_repopulate_args


def test_resolve_args_from_config():
    cfg = {
        "repo": {"path": "/repos/mall-swarm", "project_id": "mall-swarm"},
        "knowledge": {"vectordb-code": {
            "weaviate_url": "http://localhost:8080", "weaviate_grpc_port": 50051,
            "weaviate_api_key": "k", "collection_name": "CodeEntity", "dimension": 1024,
        }},
    }
    args = resolve_repopulate_args(cfg)
    assert args["repo_root"] == "/repos/mall-swarm"
    assert args["project_id"] == "mall-swarm"
    assert args["weaviate"]["collection_name"] == "CodeEntity"
    assert args["weaviate"]["dimension"] == 1024
```

- [ ] **Step 2: 运行确认失败** → FAIL

- [ ] **Step 3: 实现**

```python
# src/integrations/codegraph/cli_repopulate.py
"""CLI：从 project.yaml 跑 CodeEntity 重灌。

用法：python -m src.integrations.codegraph.cli_repopulate --config config/project.yaml [--force]
真依赖（DashScope embed / Weaviate store）在 main() 里装配；resolve_repopulate_args 纯解析便于单测。
"""
from __future__ import annotations

from typing import Any


def resolve_repopulate_args(cfg: dict) -> dict:
    """从 project.yaml dict 解析重灌需要的参数（纯函数，便于单测）。"""
    repo = cfg.get("repo") or {}
    vc = ((cfg.get("knowledge") or {}).get("vectordb-code")) or {}
    return {
        "repo_root": repo.get("path"),              # 工程源码根（读片段 + 定位 .codegraph.db）
        "project_id": repo.get("project_id") or repo.get("path"),
        "weaviate": {
            "weaviate_url": vc.get("weaviate_url"),
            "weaviate_grpc_port": vc.get("weaviate_grpc_port"),
            "weaviate_api_key": vc.get("weaviate_api_key"),
            "collection_name": vc.get("collection_name") or "CodeEntity",
            "dimension": vc.get("dimension") or 1024,
        },
    }


def main() -> None:  # pragma: no cover  （真跑入口，需全栈，不单测）
    """装配真依赖并跑全量重灌。"""
    import argparse
    import yaml  # PyYAML，读 project.yaml
    from src.semantic.embedding import get_embeddings_batch
    from src.semantic.embedding_checkpoint import EmbeddingCheckpoint
    from src.knowledge.vector_store_weaviate import WeaviateVectorStore
    from src.integrations.codegraph.db import CodeGraphDB
    from src.integrations.codegraph.paths import codegraph_db_path
    from src.integrations.codegraph.repopulate import repopulate_code_entities

    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config/project.yaml")
    p.add_argument("--force", action="store_true", help="忽略 checkpoint 全量重 embed")
    a = p.parse_args()

    cfg = yaml.safe_load(open(a.config, encoding="utf-8"))
    args = resolve_repopulate_args(cfg)
    w = args["weaviate"]
    store = WeaviateVectorStore(
        url=w["weaviate_url"], grpc_port=w["weaviate_grpc_port"],
        collection_name=w["collection_name"], dimension=w["dimension"],
        api_key=w["weaviate_api_key"],
    )
    db = CodeGraphDB(codegraph_db_path(args["repo_root"]))
    # 复用 EmbeddingCheckpoint 做单次全量的断点续跑（id-only 够用；增量见 2a.5）
    ckpt = EmbeddingCheckpoint.load(args["project_id"], weaviate_store=store)
    skip = set() if a.force else ckpt._done  # force 时不跳过
    stats = repopulate_code_entities(
        db=db, repo_root=args["repo_root"], project_id=args["project_id"],
        embed_batch=get_embeddings_batch, store=store, skip_keys=skip,
    )
    print("重灌完成：", stats)


if __name__ == "__main__":  # pragma: no cover
    main()
```

- [ ] **Step 4: 运行确认通过**（1 passed）
- [ ] **Step 5: 提交**
```bash
git add src/integrations/codegraph/cli_repopulate.py tests/test_integrations/codegraph/test_cli_repopulate.py
git commit -m "feat(codegraph): repopulate CLI entry (config -> deps assembly)"
```

---

## Task 2a.4：mall-swarm 全量重灌实跑（⚠️ 需用户起栈）

**Files:** 无（运行 + 记录）

> 前置：用户起 Weaviate(:8080) 隧道 + DashScope key 在环境 + mall-swarm 已 `codegraph index`。

- [ ] **Step 1: 跑全量重灌**
```bash
cd /Users/java/knowledge-engineering-auth
python -m src.integrations.codegraph.cli_repopulate --config config/project.yaml --force 2>&1 | tail -20
```
Expected: 打印 `重灌完成： {'scanned': ~15625, 'embedded': N, ...}`；耗时约半小时~1小时（可中断续跑，去掉 --force 续）。

- [ ] **Step 2: 抽查 Weaviate 落库 key**
```bash
python - <<'PY'
# 确认 CodeEntity 里 entity_id 是 qualified_name 形态（含 '::'），不是 canonical_v1（method//hash）
from src.knowledge.vector_store_weaviate import WeaviateVectorStore
# 用 project.yaml 的 vectordb-code 配置构造（略，参照 cli_repopulate.main）
# 取几条，打印 entity_id，肉眼确认形如 'OmsXxx::method#(...)'
PY
```
Expected: 抽样 entity_id 含 `::`（qualified_name），无 `method//`（canonical_v1）。

- [ ] **Step 3: 记录** —— 把 scanned/embedded/耗时记到设计文档 §12 实施完成标记。

---

## Task 2a.5：增量更新（内容 hash checkpoint + 删孤儿）

**Files:** Create `src/integrations/codegraph/content_checkpoint.py`；Modify `repopulate.py`(加增量函数)；Test `test_repopulate_incremental.py`

> 解决"改方法体、key 不变 → 现有 id-only checkpoint 误判已完成 → embedding 陈旧"。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_integrations/codegraph/test_repopulate_incremental.py
"""增量更新单测：内容 hash 变才重 embed；孤儿 key 删除。"""
from src.integrations.codegraph.content_checkpoint import ContentCheckpoint


def test_content_checkpoint_detects_change(tmp_path):
    cp = ContentCheckpoint(path=str(tmp_path / "cc.json"))
    assert cp.changed("A::m#()", "code-v1") is True       # 新 key → 算变
    cp.mark("A::m#()", "code-v1")
    assert cp.changed("A::m#()", "code-v1") is False      # 内容没变 → 不重 embed
    assert cp.changed("A::m#()", "code-v2") is True       # 内容变了 → 重 embed


def test_content_checkpoint_orphans(tmp_path):
    cp = ContentCheckpoint(path=str(tmp_path / "cc.json"))
    cp.mark("A::a#()", "x"); cp.mark("A::b#()", "y")
    # 当前 CodeGraph 只剩 A::a → A::b 是孤儿（被删/改名）
    assert cp.orphans({"A::a#()"}) == {"A::b#()"}
```

- [ ] **Step 2: 运行确认失败** → FAIL

- [ ] **Step 3: 实现**

```python
# src/integrations/codegraph/content_checkpoint.py
"""内容感知 checkpoint：记 {durable_key: code_snippet 的 hash}。

与现有 id-only EmbeddingCheckpoint 的区别：它能发现"key 没变但代码改了"，
从而正确触发重 embed；还能算出孤儿 key（CodeGraph 里已没有的）用于删库。
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Optional


def code_hash(snippet: str) -> str:
    """code_snippet 的 sha256（截断 16 位足够区分）。"""
    return hashlib.sha256(snippet.encode("utf-8")).hexdigest()[:16]


class ContentCheckpoint:
    """{durable_key: code_hash} 的本地持久化。"""

    def __init__(self, path: str) -> None:
        self._path = path
        self._map: dict[str, str] = {}
        if os.path.exists(path):                    # 有旧文件就加载
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    self._map = json.load(fh)
            except (OSError, ValueError):
                self._map = {}                      # 损坏 → 当空，全量重来

    def changed(self, key: str, snippet: str) -> bool:
        """该 key 的代码是否变了（或全新）→ 需要重 embed。"""
        return self._map.get(key) != code_hash(snippet)

    def mark(self, key: str, snippet: str) -> None:
        """记下该 key 当前内容 hash（embed 成功后调）。"""
        self._map[key] = code_hash(snippet)

    def orphans(self, current_keys: set[str]) -> set[str]:
        """已记录但当前 CodeGraph 里已不存在的 key（方法删/改名）→ 该删库。"""
        # 集合差：记录里有、当前没有的
        return set(self._map.keys()) - current_keys

    def drop(self, key: str) -> None:
        """从 checkpoint 移除某 key（删库后调）。"""
        self._map.pop(key, None)

    def flush(self) -> None:
        """写盘（原子：先写临时文件再 rename）。"""
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._map, fh, ensure_ascii=False)
        os.replace(tmp, self._path)                 # 原子替换
```

在 `repopulate.py` 加增量函数（复用 read_snippet/durable_key；store 需有 `delete`/`add`）：

```python
def repopulate_incremental(
    *,
    db: CodeGraphDB,
    repo_root: str,
    project_id: str,
    embed_batch: "EmbedBatch",
    store: Any,
    checkpoint: Any,            # ContentCheckpoint
    batch_size: int = 10,
) -> dict:
    """增量重灌：只 embed 内容变化/新增的方法；删 CodeGraph 已不存在的孤儿。

    store 需有 add(...) 与 delete(entity_id, *, tenant)（删孤儿用；无 delete 则只记日志）。
    """
    pending: list[tuple[str, str, str]] = []
    current_keys: set[str] = set()
    for node in db.iter_method_nodes():
        key = durable_key(node)
        current_keys.add(key)
        snippet = read_snippet(repo_root, node.file_path, node.start_line, node.end_line)
        if not snippet:
            continue
        if checkpoint.changed(key, snippet):        # 内容变了/新 key → 待重 embed
            pending.append((key, node.name, snippet))

    embedded = 0
    for i in range(0, len(pending), batch_size):
        chunk = pending[i:i + batch_size]
        vecs = embed_batch([s for _, _, s in chunk])
        for (key, name, snip), vec in zip(chunk, vecs):
            store.add(key, vec, entity_type="method", name=name, code_snippet=snip, tenant=project_id)
            checkpoint.mark(key, snip)
            embedded += 1

    # 删孤儿（方法删/改名 → 旧 key 不再出现在 CodeGraph）
    deleted = 0
    delete_method = getattr(store, "delete", None)
    for orphan in checkpoint.orphans(current_keys):
        if callable(delete_method):
            delete_method(orphan, tenant=project_id)
        checkpoint.drop(orphan)
        deleted += 1

    checkpoint.flush()
    return {"embedded": embedded, "deleted": deleted, "current": len(current_keys)}
```

> 注：`WeaviateVectorStore` 当前可能没有 `delete` 方法——若无，本任务**附带给它加一个** `delete(entity_id, *, tenant)`（按 `_to_uuid(entity_id)` 删；实现时确认 BaseWeaviateStore 是否已有 uuid 删除工具）。这是本任务的一部分，不留占位。

- [ ] **Step 4: 运行确认通过**（2 passed）+ 全量 `pytest tests/test_integrations/ -q`
- [ ] **Step 5: 提交**
```bash
git add src/integrations/codegraph/content_checkpoint.py src/integrations/codegraph/repopulate.py src/knowledge/vector_store_weaviate.py tests/test_integrations/codegraph/test_repopulate_incremental.py
git commit -m "feat(codegraph): incremental CodeEntity update (content-hash + orphan delete)"
```

---

## Task 2a.6：增量实跑验证（⚠️ 需用户起栈）

- [ ] **Step 1**：改 mall-swarm 一个方法体 → `cd /Users/java/repos/mall-swarm && codegraph sync` → 跑增量重灌（接 ContentCheckpoint）。
- [ ] **Step 2**：确认**只有那个方法被重 embed**（日志 embedded=1 量级），其余跳过；删一个方法 → 确认孤儿被删。
- [ ] **Step 3**：记录到设计文档。

---

## Self-Review

**1. Spec 覆盖（§10 Phase 2 的 2a 部分）：** 重灌 CodeEntity 按 qualified_name(2a.2/2a.3) ✅；源码片段读取(2a.1) ✅；增量更新（内容 hash + 删孤儿，对应设计"代码更新怎么更新"）2a.5 ✅；真跑 2a.4/2a.6 ✅。2b 解读重生 / 2c 检索器接通 = 明确不在本计划（后续）。

**2. 占位符扫描：** 2a.5 的"store 若无 delete 则本任务附带加"是**明确的实现指令**（含实现方式），非占位；其余均有完整代码。2a.4/2a.6 是需栈的实跑步骤，命令具体。

**3. 类型一致性：** `iter_method_nodes()->list[CgNode]`、`read_snippet(repo_root,file_path,start,end)->str`、`repopulate_code_entities(*, db, repo_root, project_id, embed_batch, store, batch_size, skip_keys)->dict`、`ContentCheckpoint.changed/mark/orphans/drop/flush`、`repopulate_incremental(...)` 全计划一致；复用 Phase 1 的 `durable_key(node)->str` 与 `CodeGraphDB`。

**已知依赖：** 2a.4/2a.6 需 Weaviate(:8080)+DashScope+mall-swarm codegraph index；2a.1/2a.2/2a.3/2a.5 纯本地（假 embed/store）可跑。
