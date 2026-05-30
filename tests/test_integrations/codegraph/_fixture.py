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
    # 3 个方法节点
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
