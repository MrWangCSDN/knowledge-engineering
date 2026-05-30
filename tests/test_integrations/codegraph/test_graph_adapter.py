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
    # 传入 durable_key（方法 = qualified_name + '#' + 参数签名）；Service.generateOrder 签名 'Map (OrderParam)'
    out = a.successors("OmsService::generateOrder#(OrderParam)")
    assert "OmsOrderDao::save#(OmsOrder)" in out


def test_predecessors_returns_durable_keys(tmp_path):
    a = _adapter(tmp_path)
    out = a.predecessors("OmsService::generateOrder#(OrderParam)")
    assert "OmsCtrl::generateOrder#(OrderParam)" in out


def test_missing_db_degrades_to_empty():
    # 库文件不存在时，导航优雅降级为空列表（不抛异常）—— 对齐设计 §8
    from src.integrations.codegraph.db import CodeGraphDB
    ad = CodeGraphGraphAdapter(CodeGraphDB("/nonexistent/path/codegraph.db"))
    assert ad.successors("Foo::bar") == []
    assert ad.predecessors("Foo::bar") == []
