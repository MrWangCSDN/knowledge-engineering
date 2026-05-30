# tests/test_integrations/codegraph/test_db.py
"""CodeGraphDB 只读访问单测：用迷你夹具库验证查询。"""
from tests.test_integrations.codegraph._fixture import make_fixture_db
from src.integrations.codegraph.db import CodeGraphDB


def test_find_by_qualified_name_and_successors(tmp_path):
    db_path = str(tmp_path / "codegraph.db")  # pytest 临时目录
    make_fixture_db(db_path)
    db = CodeGraphDB(db_path)

    svc = db.find_nodes_by_qualified_name("OmsService::generateOrder")
    assert len(svc) == 1 and svc[0].id == "n2" and svc[0].signature == "Map (OrderParam)"

    callees = db.successors("n2", "calls")
    assert [c.qualified_name for c in callees] == ["OmsOrderDao::save"]

    callers = db.predecessors("n2", "calls")
    assert [c.qualified_name for c in callers] == ["OmsCtrl::generateOrder"]


def test_get_node_and_missing(tmp_path):
    db_path = str(tmp_path / "codegraph.db")
    make_fixture_db(db_path)
    db = CodeGraphDB(db_path)
    assert db.get_node("n3").name == "save"
    assert db.get_node("nope") is None
