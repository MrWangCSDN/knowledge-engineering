# tests/test_integrations/codegraph/test_successors_with_locations.py
"""db.successors_with_locations：出边带调用点 line/col、不去重。设计 [[代码片段查看器-设计]] §4。"""
import sqlite3  # 标准库 SQLite，测试中直接造重复调用点的 DB 用

# 被测方法所在的数据访问层
from src.integrations.codegraph.db import CodeGraphDB
# 公共夹具：造迷你 .codegraph.db（n1→n2→n3 调用链）
from tests.test_integrations.codegraph._fixture import make_fixture_db


def test_returns_callee_with_line_col(tmp_path):
    """基础场景：n1 --calls(41,8)--> n2，能拿到 callee 节点 + 正确的 line/col。

    tmp_path 是 pytest 内置夹具，每个测试提供独立的临时目录（测试结束自动清理）。
    """
    # 在临时目录建迷你库
    db_path = str(tmp_path / "mini.codegraph.db")
    make_fixture_db(db_path)

    # 实例化只读访问层
    db = CodeGraphDB(db_path)

    # 查 n1 的调用出边（n1 只调用了 n2 一次，应返回长度为 1 的列表）
    rows = db.successors_with_locations("n1", "calls")   # n1 --calls(41,8)--> n2

    # 断言：只有 1 条出边
    assert len(rows) == 1

    # Python 解包（tuple unpacking）：把三元组 (CgNode, line, col) 拆开分别断言
    node, line, col = rows[0]

    # 确认被调用方是 OmsService::generateOrder
    assert node.qualified_name == "OmsService::generateOrder"

    # 确认调用点位置
    assert (line, col) == (41, 8)


def test_leaf_has_no_successors(tmp_path):
    """叶节点场景：n3 没有出边，应返回空列表。"""
    db_path = str(tmp_path / "mini.codegraph.db")
    make_fixture_db(db_path)

    db = CodeGraphDB(db_path)

    # n3 是叶节点（没有调用任何方法），期望空列表
    assert db.successors_with_locations("n3", "calls") == []


def test_not_deduped_keeps_each_call_site(tmp_path):
    """不去重场景：同一目标被调用两次，应返回 2 行（每个调用点各一行）。

    验证 successors_with_locations 与 successors 的关键区别——
    successors 会去重（同一目标只出现一次），本方法不去重。
    """
    # 手造一个含重复调用点的小库（不复用 make_fixture_db，因为场景不同）
    db_path = str(tmp_path / "dup.db")

    # sqlite3.connect：打开（或新建）SQLite 数据库文件
    conn = sqlite3.connect(db_path)

    # executescript：一次性执行多条 DDL 语句（CREATE TABLE）
    conn.executescript(
        """
        CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT, language TEXT, start_line INTEGER, end_line INTEGER, signature TEXT);
        CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, target TEXT,
            kind TEXT, line INTEGER, col INTEGER);
        """
    )

    # 插入两个方法节点：a 调用 b 两次（在不同代码行）
    conn.executemany(
        "INSERT INTO nodes(id,kind,name,qualified_name,file_path,language,start_line,end_line,signature) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [("a", "method", "m", "A::m", "A.java", "java", 1, 20, "void ()"),
         ("b", "method", "h", "B::h", "B.java", "java", 5, 8, "int ()")],
    )

    # 插入两条 calls 边：a→b 调用了两次，分别在 (3,4) 和 (10,6)
    conn.executemany(
        "INSERT INTO edges(source,target,kind,line,col) VALUES (?,?,?,?,?)",
        [("a", "b", "calls", 3, 4), ("a", "b", "calls", 10, 6)],
    )

    conn.commit()   # 提交事务写入文件
    conn.close()    # 关闭连接

    # 查 a 的调用出边，期望保留 2 条（不去重）
    rows = CodeGraphDB(db_path).successors_with_locations("a", "calls")

    # 断言：2 个调用点都在
    assert len(rows) == 2

    # sorted：对 (line, col) 排序后断言，不依赖查询顺序
    # 生成器表达式：从每行三元组里提取 (line, col)，等效于 [(l,c) for _, l, c in rows]
    assert sorted((l, c) for _, l, c in rows) == [(3, 4), (10, 6)]
