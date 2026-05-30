# tests/test_integrations/codegraph/test_repopulate_inputs.py
"""重灌输入单测：列方法节点 + 按行号读源码片段。"""
from tests.test_integrations.codegraph._fixture import make_fixture_db
from src.integrations.codegraph.db import CodeGraphDB
from src.integrations.codegraph.source_reader import read_snippet


def test_iter_method_nodes(tmp_path):
    db_path = str(tmp_path / "codegraph.db")
    make_fixture_db(db_path)
    db = CodeGraphDB(db_path)
    qns = sorted(n.qualified_name for n in db.iter_method_nodes())
    assert qns == ["OmsCtrl::generateOrder", "OmsOrderDao::save", "OmsService::generateOrder"]


def test_read_snippet(tmp_path):
    f = tmp_path / "Foo.java"
    f.write_text("L1\nL2\nL3\nL4\nL5\n", encoding="utf-8")
    snippet = read_snippet(str(tmp_path), "Foo.java", 2, 4)
    assert snippet == "L2\nL3\nL4"


def test_read_snippet_missing_file_returns_empty(tmp_path):
    assert read_snippet(str(tmp_path), "nope.java", 1, 3) == ""
