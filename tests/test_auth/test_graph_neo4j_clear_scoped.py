"""Neo4jGraphBackend.clear(project_id=...) 单元测试（多工程隔离 Phase1-T1）。

Mock-based，不连真 Neo4j。验证：
  - clear(project_id="p1") 的 Cypher 含 {project_id: $pid} 过滤，且参数 pid=="p1"
  - clear()（无参）走全库 MATCH (n:Entity) DETACH DELETE n，不含 project_id 过滤

复用 test_graph_neo4j_batch.py 的 fake session/driver 模式，不另造 mock 框架。
"""
# unittest.mock：标准库 mock 工具，patch.object 临时替换方法、MagicMock 造假对象
from unittest.mock import MagicMock, patch

# 被测后端
from src.knowledge.graph_neo4j import Neo4jGraphBackend


def _make_backend(project_id=None) -> Neo4jGraphBackend:
    """构造一个 Neo4jGraphBackend，但 mock 掉 _ensure_driver 避免真连。

    与 test_graph_neo4j_batch.py 中的同名 helper 行为一致（沿用现有测试风格）。
    """
    # patch.object 作为上下文管理器：with 块内把 _ensure_driver 替换成空 lambda，
    # 退出 with 后自动还原；这样构造对象时不会真去连 Neo4j
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


def _capture_runs(backend: Neo4jGraphBackend) -> list:
    """把 session.run 调用记录下来，返回 [(cypher, args_tuple, kwargs_dict), ...]。

    比 batch 版本多记一个位置参数 args，因为 session.run 的参数字典既可能
    走位置参数 `run(q, {"pid": ...})`，也可能走关键字 `run(q, pid=...)`；
    测试两种都要能断言到，所以同时捕获 args 和 kwargs。
    """
    # 普通 list，闭包里 append；类型注解省略以保持简单
    runs = []
    fake_session = MagicMock()

    # side_effect 函数：每次 session.run 被调用就记录一条
    def _record_run(cypher, *args, **kwargs):
        runs.append((cypher, args, kwargs))
        return MagicMock()  # 假 result，clear 不读返回值

    fake_session.run = _record_run
    # `with self._driver.session(...) as session:` 走 ContextManager 协议，
    # __enter__ 返回的就是我们的 fake_session
    backend._driver.session.return_value.__enter__.return_value = fake_session
    return runs


def _params_of(run_record) -> dict:
    """从一条 run 记录里取出"传给 Cypher 的参数字典"。

    兼容两种调用风格：
      - 位置参数：session.run(query, {"pid": "p1"})  -> args[0] 是 dict
      - 关键字参数：session.run(query, pid="p1")       -> kwargs 里有 pid
    返回合并后的参数 dict。
    """
    _cypher, args, kwargs = run_record
    params = {}
    # 若第一个位置参数是 dict，认为是参数字典
    if args and isinstance(args[0], dict):
        params.update(args[0])
    # 关键字参数也并进来
    params.update(kwargs)
    return params


def test_clear_scoped_filters_by_project_id():
    """clear(project_id="p1")：Cypher 含 project_id 过滤，且参数化 pid=="p1"。"""
    b = _make_backend()
    runs = _capture_runs(b)
    b.clear(project_id="p1")

    # 只应跑一条 clear 语句
    assert len(runs) == 1, f"应该 1 次 query；实际 {len(runs)}"
    cypher, _args, _kwargs = runs[0]
    # 1) 标签仍是 Entity
    assert "MATCH (n:Entity" in cypher
    # 2) 走 project_id 过滤（内联 map 形式 {project_id: $pid}）
    assert "project_id" in cypher
    # 3) 必须参数化：用 $pid 占位，禁止把 "p1" 字面量拼进 Cypher
    assert "$pid" in cypher
    assert "p1" not in cypher, "project_id 不能字符串拼接进 Cypher，必须走参数"
    # 4) 仍是删除语义
    assert "DETACH DELETE" in cypher
    # 5) 参数里 pid == "p1"
    params = _params_of(runs[0])
    assert params.get("pid") == "p1"


def test_clear_no_arg_is_full_wipe():
    """clear()（无参）：全库删除，不含 project_id 过滤（向后兼容）。"""
    b = _make_backend()
    runs = _capture_runs(b)
    b.clear()

    assert len(runs) == 1, f"应该 1 次 query；实际 {len(runs)}"
    cypher, _args, _kwargs = runs[0]
    # 全库语句：MATCH (n:Entity) DETACH DELETE n
    assert "MATCH (n:Entity) DETACH DELETE n" in cypher
    # 不应出现 project_id 过滤
    assert "project_id" not in cypher
    assert "$pid" not in cypher


def test_clear_none_is_full_wipe():
    """clear(project_id=None)：显式 None 也走全库删除（与无参一致）。"""
    b = _make_backend()
    runs = _capture_runs(b)
    b.clear(project_id=None)

    assert len(runs) == 1
    cypher, _args, _kwargs = runs[0]
    assert "MATCH (n:Entity) DETACH DELETE n" in cypher
    assert "project_id" not in cypher


def test_clear_empty_string_is_full_wipe():
    """clear(project_id="")：空字符串视为假值，走全库删除（与现有 _project_id 真值判断一致）。"""
    b = _make_backend()
    runs = _capture_runs(b)
    b.clear(project_id="")

    assert len(runs) == 1
    cypher, _args, _kwargs = runs[0]
    assert "MATCH (n:Entity) DETACH DELETE n" in cypher
    assert "project_id" not in cypher
