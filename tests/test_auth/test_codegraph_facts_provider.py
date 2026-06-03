"""CodeGraphFactsProvider 单测：CodeGraph → StructureFacts，id=qualified_name。

设计 [[2b解读重生-设计]] §4.1；计划 docs/superpowers/plans/2026-06-03-2b-codegraph-interpretation.md Task 1。
自建最小 .codegraph.db（schema 对齐 src/integrations/codegraph/db.py 的 _COLS / kind 约定），不依赖外部夹具。
"""
# sqlite3：标准库，手建最小 .codegraph.db（nodes/edges 两表）
import sqlite3

# 被测产物：枚举 + Provider
from src.models.structure import EntityType, RelationType
from src.knowledge.codegraph_facts_provider import CodeGraphFactsProvider


def _mini_db(path) -> None:
    """手建最小 .codegraph.db：2 个业务层 method + 1 个 mbg method + 2 条 calls 边。

    列顺序严格对齐 db.py 的 _COLS：id,kind,name,qualified_name,file_path,start_line,end_line,signature。
    """
    # sqlite3.connect(str(path))：建库文件；str() 因 path 可能是 pathlib.Path
    conn = sqlite3.connect(str(path))
    # nodes 表：CodeGraphDB._row_to_node 按 _COLS 列名读取
    conn.execute(
        "CREATE TABLE nodes (id TEXT, kind TEXT, name TEXT, qualified_name TEXT, "
        "file_path TEXT, start_line INT, end_line INT, signature TEXT)"
    )
    # edges 表：successors 用 source/target/kind（+line/col，本测试不校验位置）
    conn.execute("CREATE TABLE edges (source TEXT, target TEXT, kind TEXT, line INT, col INT)")
    # 业务层入口 Controller（mall-portal）
    conn.execute(
        "INSERT INTO nodes VALUES "
        "('n1','method','generateOrder','OmsPortalOrderController::generateOrder',"
        "'mall-portal/src/main/java/Ctrl.java',10,20,'(OrderParam)')"
    )
    # 业务层 ServiceImpl（mall-portal）
    conn.execute(
        "INSERT INTO nodes VALUES "
        "('n2','method','generateOrder','OmsPortalOrderServiceImpl::generateOrder',"
        "'mall-portal/src/main/java/Svc.java',30,60,'(OrderParam)')"
    )
    # mbg 方法（Phase1 module_filter 应过滤掉，不单独成 entity）
    conn.execute(
        "INSERT INTO nodes VALUES "
        "('n3','method','insert','OmsOrderMapper::insert',"
        "'mall-mbg/src/main/java/M.java',5,8,'(OmsOrder)')"
    )
    conn.execute("INSERT INTO edges VALUES ('n1','n2','calls',12,4)")  # ctrl → svc
    conn.execute("INSERT INTO edges VALUES ('n2','n3','calls',40,8)")  # svc → mbg mapper
    conn.commit()
    conn.close()


def test_build_facts_method_id_is_qualified_name(tmp_path):
    """method 实体 id=qualified_name；片段/类名/签名/模块填充；CALLS/CONTAINS 关系按 qn；mbg 被过滤。"""
    db = tmp_path / "x.codegraph.db"
    _mini_db(db)
    repo = tmp_path / "repo"
    # 造源文件让 read_snippet 能读到（行 10-20 / 30-60 都在 60 行内）
    (repo / "mall-portal/src/main/java").mkdir(parents=True)
    # "\n".join(...)：拼出 60 行文本（line0..line59）
    (repo / "mall-portal/src/main/java/Ctrl.java").write_text(
        "\n".join(f"line{i}" for i in range(60))
    )
    (repo / "mall-portal/src/main/java/Svc.java").write_text(
        "\n".join(f"line{i}" for i in range(60))
    )

    provider = CodeGraphFactsProvider(db_path=str(db), repo_local_path=str(repo))
    # Phase1：只业务层模块
    facts = provider.build_structure_facts(module_filter={"mall-portal"})

    methods = [e for e in facts.entities if e.type == EntityType.METHOD]
    ids = {e.id for e in methods}
    # ① method 实体 id = qualified_name
    assert "OmsPortalOrderController::generateOrder" in ids
    assert "OmsPortalOrderServiceImpl::generateOrder" in ids
    # ② mbg 方法被 module_filter 过滤，不作为 entity
    assert "OmsOrderMapper::insert" not in ids
    # ③ 片段 + 类名 + 签名 + 模块
    ctrl = next(e for e in methods if e.id == "OmsPortalOrderController::generateOrder")
    assert ctrl.attributes["code_snippet"].strip() != ""
    assert ctrl.attributes["class_name"] == "OmsPortalOrderController"
    assert ctrl.attributes["signature"] == "(OrderParam)"
    assert ctrl.module_id == "mall-portal"
    # ④ CALLS 关系按 qualified_name（含指向被过滤的 mbg 目标，供解读文本引用）
    calls = [(r.source_id, r.target_id) for r in facts.relations if r.type == RelationType.CALLS]
    assert ("OmsPortalOrderController::generateOrder", "OmsPortalOrderServiceImpl::generateOrder") in calls
    assert ("OmsPortalOrderServiceImpl::generateOrder", "OmsOrderMapper::insert") in calls
    # ⑤ CONTAINS：class → method（让解读器解析 class_entity_id）
    contains = [(r.source_id, r.target_id) for r in facts.relations if r.type == RelationType.CONTAINS]
    assert ("OmsPortalOrderController", "OmsPortalOrderController::generateOrder") in contains


def test_snippet_unreadable_yields_empty_not_crash(tmp_path):
    """源文件缺失 → code_snippet="" 不抛异常（解读器会 _filter_meaningful 跳过）。"""
    db = tmp_path / "x.codegraph.db"
    _mini_db(db)
    # repo_local_path 指向不存在目录 → read_snippet 读不到 → 降级空串
    provider = CodeGraphFactsProvider(db_path=str(db), repo_local_path=str(tmp_path / "norepo"))
    facts = provider.build_structure_facts(module_filter={"mall-portal"})
    ctrl = next(
        e for e in facts.entities if e.id == "OmsPortalOrderController::generateOrder"
    )
    assert ctrl.attributes["code_snippet"] == ""  # 读不到→空，不崩
