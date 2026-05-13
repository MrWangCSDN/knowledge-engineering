"""v2.0 Neo4jGraphAdapter 多租户隔离测试。

验证：
  1. 构造时不传 project_id → TypeError
  2. 空字符串 project_id → TypeError
  3. successors 的 Cypher 带 project_id 参数
  4. predecessors 的 Cypher 带 project_id 参数
  5. rel_type 参数正确透传
"""
# pytest：Python 主流测试框架，提供 fixtures / assert 重写 / 参数化等能力
import pytest
# MagicMock：mock 模块的"万能替身"，访问任意属性/方法都不报错，也不真正运行
from unittest.mock import MagicMock

# 被测类
from src.service.qa_engine.adapters import Neo4jGraphAdapter


# ─── 构造函数校验 ────────────────────────────────────────────────────────────

def test_neo4j_adapter_requires_project_id():
    """v2.0：构造时不传 project_id 应该 TypeError。"""
    # MagicMock() 生成一个"假 backend"，不需要真正连 Neo4j
    backend = MagicMock()
    # pytest.raises(TypeError) 是上下文管理器：断言 with 块内必须抛出 TypeError
    # match="project_id" 额外验证错误信息包含 "project_id" 字样
    with pytest.raises(TypeError, match="project_id"):
        # 缺省 project_id 参数：Python 会在运行时报 TypeError（参数不足）
        # 但我们自己的检查会先触发（空值检查在 __init__ 里）
        Neo4jGraphAdapter(backend=backend)


def test_neo4j_adapter_rejects_empty_project_id():
    """空字符串也应被拒（等价于未提供）。"""
    backend = MagicMock()
    with pytest.raises(TypeError, match="project_id"):
        # "" 在 Python 中是 falsy：bool("") == False
        Neo4jGraphAdapter(backend=backend, project_id="")


# ─── successors ─────────────────────────────────────────────────────────────

def test_successors_filters_by_project_id():
    """successors 应以 project_id 为参数调用 Cypher，且结果正确。"""
    backend = MagicMock()
    # 模拟 backend._driver.session() 上下文管理器的行为：
    # __enter__ 返回 fake_session（即 with ... as s: 里的 s）
    fake_session = MagicMock()
    backend._driver.session.return_value.__enter__.return_value = fake_session
    # run() 的返回值：两行假数据（每行是 dict，模拟 Neo4j Record 的 .get 行为）
    fake_session.run.return_value = [{"nid": "method//abc"}, {"nid": "class//xyz"}]

    # 构造 adapter，绑定 project_id="petclinic"
    adapter = Neo4jGraphAdapter(backend=backend, project_id="petclinic")
    result = adapter.successors("method//start")

    # 断言结果列表内容正确
    assert result == ["method//abc", "class//xyz"]

    # 验证 Cypher 调用：call_args 是 mock 框架记录的最后一次调用参数
    call_args = fake_session.run.call_args
    # kwargs 里 pid 必须等于构造时的 project_id
    assert call_args.kwargs.get("pid") == "petclinic"
    # eid 必须等于传入的 entity_id
    assert call_args.kwargs.get("eid") == "method//start"
    # Cypher 字符串（call_args.args[0]）必须含 "project_id"，证明有隔离过滤
    cypher = call_args.args[0]
    assert "project_id" in cypher


# ─── predecessors ────────────────────────────────────────────────────────────

def test_predecessors_filters_by_project_id():
    """predecessors 同样须带 project_id 参数。"""
    backend = MagicMock()
    fake_session = MagicMock()
    backend._driver.session.return_value.__enter__.return_value = fake_session
    fake_session.run.return_value = [{"nid": "method//caller"}]

    adapter = Neo4jGraphAdapter(backend=backend, project_id="petclinic")
    result = adapter.predecessors("method//target")

    assert result == ["method//caller"]
    call_args = fake_session.run.call_args
    # 同样验证 pid 参数正确透传
    assert call_args.kwargs.get("pid") == "petclinic"


# ─── rel_type 参数 ───────────────────────────────────────────────────────────

def test_successors_with_rel_type():
    """rel_type 参数须正确透传到 Cypher 的 $rel 参数。"""
    backend = MagicMock()
    fake_session = MagicMock()
    backend._driver.session.return_value.__enter__.return_value = fake_session
    # 返回空列表：只测参数透传，不关心结果
    fake_session.run.return_value = []

    adapter = Neo4jGraphAdapter(backend=backend, project_id="x")
    # 显式传 rel_type="CALLS"
    adapter.successors("eid", rel_type="CALLS")

    call_args = fake_session.run.call_args
    # $rel 对应 kwargs["rel"]，应等于 "CALLS"
    assert call_args.kwargs.get("rel") == "CALLS"
