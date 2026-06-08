"""adapter 解析三原语（resolve_at_position / find_by_name / resolve_impl）单测。

设计 [[代码查看器-IDE化导航-设计]] §4.1（Phase-0 已核实 schema：edges 带 line/col、
nodes_fts 全文索引、implements 真边）。Task 1 / Step 2。

自建最小 .codegraph.db（schema 对齐生产 db.py 的 _COLS + nodes_fts 虚拟表 + edges 完整列），
不依赖外部夹具——所有测试仅靠 sqlite3 + tmp_path 跑起来。
"""
# sqlite3：标准库，手建最小 .codegraph.db（nodes/edges/nodes_fts 三表）
import sqlite3

# pytest 内置夹具 tmp_path 由参数自动注入；不必显式 import
import pytest

# 被测产物：DB 层 + adapter 层（Task 1 实现）
from src.integrations.codegraph.db import CodeGraphDB
from src.integrations.codegraph.graph_adapter import CodeGraphGraphAdapter

# (file, line, col) 约定：Bar.java:15:8 处一处 references 边指向 Foo 类型
# 测试用常量，避免散落"魔法数字"在多个 assert 里
_REF_FILE = "Bar.java"   # 持有 references 出边的方法所在文件
_REF_LINE = 15           # 该 references 边在源码中的行号（edges.line）
_REF_COL = 8             # 该 references 边在源码中的列号（edges.col）


def _mini_db(path) -> None:
    """手建最小 .codegraph.db：覆盖三原语场景。

    nodes/edges 列对齐生产 schema（_COLS + edges 完整列）；
    nodes_fts 虚拟表用 FTS5，存 (id, name, qualified_name)，模拟生产全文索引。

    数据布局：
      ① 方法 Bar::callsFoo（n_m_bar） — 在 Bar.java 内
         有一条 references 边指向类 Foo（n_c_foo），edge 位置 = (Bar.java, 15, 8)
      ② 接口 ISvc（n_c_iface） + 接口方法 ISvc::m（n_m_iface）
         实现类 SvcImpl（n_c_impl） + 实现方法 SvcImpl::m（n_m_impl）
         一条 contains 边 ISvc → ISvc::m；一条 contains 边 SvcImpl → SvcImpl::m
         一条 implements 边 SvcImpl → ISvc（按 CodeGraph 实际方向：impl 类 → 接口类）
    """
    # 注意：connect(str(path)) 这里传 path 是 pathlib.Path，必须 str() 一下
    conn = sqlite3.connect(str(path))

    # nodes 表：对齐 db.py 的 _COLS 列顺序，便于 _row_to_node 按名取值
    conn.execute(
        "CREATE TABLE nodes (id TEXT, kind TEXT, name TEXT, qualified_name TEXT, "
        "file_path TEXT, start_line INT, end_line INT, signature TEXT)"
    )
    # edges 表：source/target/kind 是导航必备；line/col 用于位置精确解析
    conn.execute(
        "CREATE TABLE edges (source TEXT, target TEXT, kind TEXT, line INT, col INT)"
    )
    # nodes_fts 虚拟表（FTS5）：模拟生产 schema 的全文索引
    # id UNINDEXED：让 id 列可读但不参与匹配，便于子查询 SELECT id FROM nodes_fts ... 回主表
    # name / qualified_name：参与 FTS MATCH（CamelCase 用户输入大概率落在这两列）
    conn.execute(
        "CREATE VIRTUAL TABLE nodes_fts USING fts5(id UNINDEXED, name, qualified_name)"
    )

    # ── 场景①：方法 Bar::callsFoo 引用类型 Foo（位置解析覆盖 references 边） ──
    # 方法节点 Bar::callsFoo（kind=method；有 signature → durable_key 拼 '#()'）
    conn.execute(
        "INSERT INTO nodes VALUES "
        "('n_m_bar','method','callsFoo','Bar::callsFoo',?,10,20,'()')",
        (_REF_FILE,),  # file_path 参数化，与 _REF_FILE 常量一致
    )
    # 类节点 Foo（kind=class；类不重载，durable_key=qualified_name 即 'Foo'）
    conn.execute(
        "INSERT INTO nodes VALUES "
        "('n_c_foo','class','Foo','Foo','Foo.java',1,30,NULL)"
    )
    # references 边：Bar::callsFoo 内部 (15, 8) 处引用了 Foo 类型
    conn.execute(
        "INSERT INTO edges VALUES ('n_m_bar','n_c_foo','references',?,?)",
        (_REF_LINE, _REF_COL),
    )

    # ── 场景②：接口 ISvc 与实现 SvcImpl（接口→实现解析） ──
    # 接口类 ISvc（kind=interface）
    conn.execute(
        "INSERT INTO nodes VALUES "
        "('n_c_iface','interface','ISvc','ISvc','ISvc.java',1,10,NULL)"
    )
    # 接口方法 ISvc::m（kind=method）
    conn.execute(
        "INSERT INTO nodes VALUES "
        "('n_m_iface','method','m','ISvc::m','ISvc.java',5,6,'()')"
    )
    # 实现类 SvcImpl（kind=class，普通类）
    conn.execute(
        "INSERT INTO nodes VALUES "
        "('n_c_impl','class','SvcImpl','SvcImpl','SvcImpl.java',1,30,NULL)"
    )
    # 实现方法 SvcImpl::m（kind=method）
    conn.execute(
        "INSERT INTO nodes VALUES "
        "('n_m_impl','method','m','SvcImpl::m','SvcImpl.java',5,20,'()')"
    )
    # contains 边：ISvc 类 → ISvc::m 方法（接口类包含接口方法）
    conn.execute("INSERT INTO edges VALUES ('n_c_iface','n_m_iface','contains',NULL,NULL)")
    # contains 边：SvcImpl 类 → SvcImpl::m 方法（实现类包含实现方法）
    conn.execute("INSERT INTO edges VALUES ('n_c_impl','n_m_impl','contains',NULL,NULL)")
    # implements 边：SvcImpl 实现 ISvc（生产 schema：source=impl 类、target=接口类）
    conn.execute("INSERT INTO edges VALUES ('n_c_impl','n_c_iface','implements',NULL,NULL)")

    # ── 同步 nodes_fts：每个 node 插一行（id 不变；name + qualified_name 进索引） ──
    # 真实生产可能通过触发器自动同步，但单测里手动插入更可控、调试更清晰
    for nid, name, qn in [
        ("n_m_bar", "callsFoo", "Bar::callsFoo"),
        ("n_c_foo", "Foo", "Foo"),
        ("n_c_iface", "ISvc", "ISvc"),
        ("n_m_iface", "m", "ISvc::m"),
        ("n_c_impl", "SvcImpl", "SvcImpl"),
        ("n_m_impl", "m", "SvcImpl::m"),
    ]:
        conn.execute(
            "INSERT INTO nodes_fts(id, name, qualified_name) VALUES (?, ?, ?)",
            (nid, name, qn),
        )

    # commit 持久化所有 INSERT；close 释放文件句柄（后续 _connect 以只读 URI 重开）
    conn.commit()
    conn.close()


@pytest.fixture()
def adapter(tmp_path):
    """构造 CodeGraphGraphAdapter（包了 CodeGraphDB），指向 tmp_path 下的最小 db。"""
    # tmp_path / 'x.codegraph.db' 用 pathlib 的 / 运算符拼路径，跨平台
    db_path = tmp_path / "x.codegraph.db"
    _mini_db(db_path)
    # CodeGraphDB 接受字符串路径；__init__ 不打开连接，方法调用时再连
    return CodeGraphGraphAdapter(CodeGraphDB(str(db_path)))


# ───────────────────────────────────────────────────────────────────────────────
# 测试 1：位置→边命中 — 给定 (file, line, col) → 返回 references 边的 target durable_key
# ───────────────────────────────────────────────────────────────────────────────
def test_edges_at_returns_target_for_position(adapter):
    """(Bar.java, 15, 8) 落在 Bar::callsFoo 的 references 边上 → 返回 Foo 的 durable_key。"""
    # resolve_at_position 是 adapter 暴露给 Task 2 的解析原语；返回 durable_key 或 None
    hit = adapter.resolve_at_position(_REF_FILE, _REF_LINE, _REF_COL)
    # Foo 类不重载，durable_key 即 qualified_name='Foo'；用 in 而非 == 给实现少量容差
    assert hit is not None and "Foo" in hit


# ───────────────────────────────────────────────────────────────────────────────
# 测试 2：名字回退 — 走 nodes_fts 全文索引匹配 CamelCase token
# ───────────────────────────────────────────────────────────────────────────────
def test_find_by_name_via_fts(adapter):
    """token='Foo' → 走 nodes_fts → 结果包含 Foo 类 durable_key。"""
    # find_by_name 返回 durable_key 列表（限定 limit 防爆炸）
    hits = adapter.find_by_name("Foo", limit=5)
    # 至少一个命中的 key 里包含 'Foo'（durable_key 既可能是 'Foo' 也可能是 'Foo::xxx'）
    assert any("Foo" in k for k in hits)


# ───────────────────────────────────────────────────────────────────────────────
# 测试 3：接口→实现 — ISvc::m 应解析到 SvcImpl::m
# ───────────────────────────────────────────────────────────────────────────────
def test_resolve_impl_iface_method_to_impl(adapter):
    """接口方法 ISvc::m（带签名后缀）→ 经 implements 边 → SvcImpl::m。"""
    # 输入 durable_key 风格：'ISvc::m#()'；resolve_impl 需要剥签名取 qualified_name
    impl_key = adapter.resolve_impl("ISvc::m#()")
    # 结果应是 SvcImpl::m 的 durable_key；用 in 容差不同实现细节
    assert impl_key is not None and "SvcImpl" in impl_key


# ───────────────────────────────────────────────────────────────────────────────
# 测试 4：非接口方法 → resolve_impl 返 None（不误改写普通方法）
# ───────────────────────────────────────────────────────────────────────────────
def test_resolve_impl_non_iface_returns_none(adapter):
    """SvcImpl::m 本身就是实现，不是接口方法 → resolve_impl 返 None（不再改写）。"""
    # 调用方应据此判断"无需改写"，仍用原 entity_id
    assert adapter.resolve_impl("SvcImpl::m#()") is None
