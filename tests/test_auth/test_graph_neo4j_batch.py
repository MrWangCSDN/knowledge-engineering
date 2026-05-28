"""Neo4jGraphBackend.add_nodes_batch / add_edges_batch 单元测试。

Mock-based，不连真 Neo4j。验证：
  - 单次 chunk：Cypher 含 UNWIND + 正确 chunk 参数
  - 多 chunk：按 chunk_size 切片，每片一次 session.run
  - project_id 自动注入
  - edges 按 rel_type 分组（关系类型不能参数化）
  - None 属性被过滤掉
  - 空输入直接 return（不调 session）
"""
# unittest.mock：标准库 mock 工具
from unittest.mock import MagicMock, patch

import pytest

from src.knowledge.graph_neo4j import Neo4jGraphBackend


def _make_backend(project_id=None) -> Neo4jGraphBackend:
    """构造一个 Neo4jGraphBackend，但 mock 掉 _ensure_driver 避免真连。"""
    # `patch.object` 作为装饰器 / 上下文管理器都行；这里用 with
    with patch.object(Neo4jGraphBackend, "_ensure_driver", lambda self: None):
        b = Neo4jGraphBackend(
            uri="bolt://fake:7687",
            user="u",
            password="p",
            project_id=project_id,
        )
    # 注入 mock driver；后续 session() 调用走 MagicMock
    b._driver = MagicMock()
    return b


def _capture_runs(backend: Neo4jGraphBackend) -> list[tuple[str, dict]]:
    """把 session.run 调用记录下来，返回 [(cypher, kwargs), ...]。

    side_effect 让我们用一个函数代替 mock 的 return_value：每次 run 被调用都触发记录。
    """
    runs: list[tuple[str, dict]] = []
    fake_session = MagicMock()

    # `session.run(cypher, **kwargs)` 或 `session.run(cypher, items=...)`
    def _record_run(cypher, **kwargs):
        runs.append((cypher, kwargs))
        return MagicMock()  # 假 result

    fake_session.run = _record_run
    # `with self._driver.session(...) as session:` 走的是 ContextManager
    backend._driver.session.return_value.__enter__.return_value = fake_session
    return runs


def test_add_nodes_batch_empty_no_op():
    """空 list → 不调 session（节省 connection 开销）。"""
    b = _make_backend()
    runs = _capture_runs(b)
    b.add_nodes_batch([])
    assert runs == []


def test_add_nodes_batch_single_chunk_uses_unwind():
    """3 个 node（< chunk_size）→ 1 次 UNWIND query。"""
    b = _make_backend(project_id="proj-x")
    runs = _capture_runs(b)
    items = [
        ("n1", {"name": "Alice", "extra": None}),  # None 应被过滤
        ("n2", {"name": "Bob"}),
        ("n3", {"name": "Carol", "entity_type": "method"}),
    ]
    b.add_nodes_batch(items)

    assert len(runs) == 1, f"应该 1 次 query；实际 {len(runs)}"
    cypher, kw = runs[0]
    # 1) Cypher 走 UNWIND，含 MERGE
    assert "UNWIND" in cypher
    assert "MERGE (n:Entity" in cypher
    # 2) items 参数有 3 条，每条带 project_id
    assert len(kw["items"]) == 3
    # 第一条：None 属性应被过滤掉
    assert "extra" not in kw["items"][0]["attrs"]
    assert kw["items"][0]["attrs"]["project_id"] == "proj-x"
    # ID 透传
    assert kw["items"][0]["id"] == "n1"


def test_add_nodes_batch_multiple_chunks():
    """1200 nodes, chunk_size=500 → 3 次 query (500+500+200)。"""
    b = _make_backend()
    runs = _capture_runs(b)
    items = [(f"n{i}", {"name": f"node-{i}"}) for i in range(1200)]
    b.add_nodes_batch(items, chunk_size=500)

    assert len(runs) == 3, f"应该 3 次 query；实际 {len(runs)}"
    # 每个 chunk 大小
    assert len(runs[0][1]["items"]) == 500
    assert len(runs[1][1]["items"]) == 500
    assert len(runs[2][1]["items"]) == 200


def test_add_nodes_batch_no_project_id_no_injection():
    """project_id=None 时不注入 project_id 属性。"""
    b = _make_backend(project_id=None)
    runs = _capture_runs(b)
    b.add_nodes_batch([("n1", {"name": "Alice"})])

    items = runs[0][1]["items"]
    # 不应自动加 project_id（legacy 模式）
    assert "project_id" not in items[0]["attrs"]


def test_add_edges_batch_empty_no_op():
    """空 list → 不调 session。"""
    b = _make_backend()
    runs = _capture_runs(b)
    b.add_edges_batch([])
    assert runs == []


def test_add_edges_batch_groups_by_rel_type():
    """边按 rel_type 分组：3 calls + 2 belongs_to → 2 次 query。"""
    b = _make_backend(project_id="proj-x")
    runs = _capture_runs(b)
    items = [
        ("a", "b", "calls", {"line": 10}),
        ("a", "c", "calls", {"line": 20}),
        ("b", "d", "belongs_to", {}),
        ("c", "e", "calls", {"line": 30}),
        ("d", "f", "belongs_to", {}),
    ]
    b.add_edges_batch(items)

    # 2 个 rel_type 分组 → 2 次 query
    assert len(runs) == 2
    # 收集 Cypher 文本，检查关系类型嵌入
    cyphers = [c for c, _ in runs]
    # _rel_type 把 "calls" 转成 "CALLS"
    assert any("CALLS" in c for c in cyphers)
    assert any("BELONGS_TO" in c for c in cyphers)


def test_add_edges_batch_attrs_injection():
    """每条边的 attrs 应含 rel_type（保留原文） + project_id（自动注入）。"""
    b = _make_backend(project_id="proj-x")
    runs = _capture_runs(b)
    items = [("a", "b", "calls", {"line": 10, "skip_this": None})]
    b.add_edges_batch(items)

    edge_row = runs[0][1]["items"][0]
    # sid / tid 透传
    assert edge_row["sid"] == "a"
    assert edge_row["tid"] == "b"
    # rel_type 在 attrs 内保留为小写原文（用于 r.rel_type 查询兼容）
    assert edge_row["attrs"]["rel_type"] == "calls"
    # project_id 自动注入
    assert edge_row["attrs"]["project_id"] == "proj-x"
    # None 属性应被过滤
    assert "skip_this" not in edge_row["attrs"]


def test_add_edges_batch_multi_chunk_within_rel_type():
    """同 rel_type 1200 条，chunk_size=500 → 3 次 query（同组内切片）。"""
    b = _make_backend()
    runs = _capture_runs(b)
    items = [(f"a{i}", f"b{i}", "calls", {}) for i in range(1200)]
    b.add_edges_batch(items, chunk_size=500)

    assert len(runs) == 3
    sizes = [len(r[1]["items"]) for r in runs]
    assert sizes == [500, 500, 200]
