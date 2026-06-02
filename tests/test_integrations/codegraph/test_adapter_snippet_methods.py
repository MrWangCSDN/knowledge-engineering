# tests/test_integrations/codegraph/test_adapter_snippet_methods.py
"""CodeGraphGraphAdapter 的代码片段三方法 + NullGraphAdapter 降级。设计 [[代码片段查看器-设计]] §4。"""
from src.integrations.codegraph.db import CodeGraphDB
from src.integrations.codegraph.graph_adapter import CodeGraphGraphAdapter
from src.integrations.codegraph.graph_factory import NullGraphAdapter
from tests.test_integrations.codegraph._fixture import make_fixture_db


def _adapter(tmp_path):
    # 在临时目录建 mini fixture 库，返回包装好的 adapter
    db_path = str(tmp_path / "mini.codegraph.db")
    make_fixture_db(db_path)
    return CodeGraphGraphAdapter(CodeGraphDB(db_path))


def test_resolve_first_returns_node(tmp_path):
    adp = _adapter(tmp_path)
    # OmsCtrl::generateOrder 在 fixture 里有对应节点，期望解析成功
    node = adp.resolve_first("OmsCtrl::generateOrder")
    assert node is not None and node.qualified_name == "OmsCtrl::generateOrder"


def test_resolve_first_none_for_unknown(tmp_path):
    # Ghost::x#() 不存在于 fixture，期望返回 None（降级）
    assert _adapter(tmp_path).resolve_first("Ghost::x#()") is None


def test_successors_with_locations(tmp_path):
    adp = _adapter(tmp_path)
    # n1(OmsCtrl::generateOrder) → n2(OmsService::generateOrder) 调用点(41,8)
    callees = adp.successors_with_locations("OmsCtrl::generateOrder")
    assert callees == [
        {"entity_id": "OmsService::generateOrder#(OrderParam)", "name": "generateOrder", "line": 41, "col": 8}
    ]


def test_callers(tmp_path):
    adp = _adapter(tmp_path)
    # n2(OmsService::generateOrder) 被 n1(OmsCtrl::generateOrder) 调用
    callers = adp.callers("OmsService::generateOrder")
    assert callers == [{"entity_id": "OmsCtrl::generateOrder#(OrderParam)", "name": "generateOrder"}]


def test_null_adapter_degrades(tmp_path):
    # NullGraphAdapter：无 CodeGraph 索引时，三方法都应优雅降级
    null = NullGraphAdapter()
    assert null.resolve_first("X::y#()") is None
    assert null.successors_with_locations("X::y#()") == []
    assert null.callers("X::y#()") == []


def test_successors_with_locations_passes_none_col(tmp_path):
    """col 为 NULL 时透传为 None（不过滤、不填默认值）。"""
    import sqlite3
    db_path = str(tmp_path / "nullcol.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, name TEXT, qualified_name TEXT, "
        "file_path TEXT, language TEXT, start_line INTEGER, end_line INTEGER, signature TEXT);"
        "CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, target TEXT, "
        "kind TEXT, line INTEGER, col INTEGER);"
    )
    conn.executemany(
        "INSERT INTO nodes(id,kind,name,qualified_name,file_path,language,start_line,end_line,signature) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [("a", "method", "m", "A::m", "A.java", "java", 1, 9, "void ()"),
         ("b", "method", "h", "B::h", "B.java", "java", 2, 3, "int ()")],
    )
    conn.execute("INSERT INTO edges(source,target,kind,line,col) VALUES ('a','b','calls',5,NULL)")
    conn.commit(); conn.close()
    adp = CodeGraphGraphAdapter(CodeGraphDB(db_path))
    callees = adp.successors_with_locations("A::m")
    assert callees == [{"entity_id": "B::h#()", "name": "h", "line": 5, "col": None}]


def test_callers_dedupes_multiple_calls(tmp_path):
    """同一 caller 多次调用 entity_id → callers 只返回一项（按 entity_id 去重）。"""
    import sqlite3
    db_path = str(tmp_path / "dupcaller.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, name TEXT, qualified_name TEXT, "
        "file_path TEXT, language TEXT, start_line INTEGER, end_line INTEGER, signature TEXT);"
        "CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, target TEXT, "
        "kind TEXT, line INTEGER, col INTEGER);"
    )
    conn.executemany(
        "INSERT INTO nodes(id,kind,name,qualified_name,file_path,language,start_line,end_line,signature) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [("a", "method", "m", "A::m", "A.java", "java", 1, 20, "void ()"),
         ("b", "method", "h", "B::h", "B.java", "java", 2, 3, "int ()")],
    )
    conn.executemany(
        "INSERT INTO edges(source,target,kind,line,col) VALUES (?,?,?,?,?)",
        [("a", "b", "calls", 3, 4), ("a", "b", "calls", 10, 6)],   # a 调 b 两次
    )
    conn.commit(); conn.close()
    adp = CodeGraphGraphAdapter(CodeGraphDB(db_path))
    callers = adp.callers("B::h")
    assert callers == [{"entity_id": "A::m#()", "name": "m"}]
