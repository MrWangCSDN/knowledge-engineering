"""验证 resolve_graph_adapter 在缺 CodeGraph 索引时优雅降级为 NullGraphAdapter，否则用真适配器。

设计 [[CodeGraph-结构引擎集成-设计]] §8：缺索引时 QA 走"语义检索+空图导航"降级，不报错。
"""
from src.integrations.codegraph.graph_factory import resolve_graph_adapter, NullGraphAdapter
from src.integrations.codegraph.graph_adapter import CodeGraphGraphAdapter


def test_null_adapter_when_repo_path_empty():
    """repo_local_path 为 None/"" → NullGraphAdapter，导航返 []（不抛 ValueError）。"""
    for empty in (None, ""):
        g = resolve_graph_adapter(empty)
        assert isinstance(g, NullGraphAdapter)
        assert g.successors("X::y") == []
        assert g.predecessors("X::y") == []


def test_null_adapter_when_db_missing(tmp_path):
    """路径有了但 .codegraph/codegraph.db 不存在 → 仍降级 NullGraphAdapter。"""
    # tmp_path 是 pytest 内置临时目录夹具；这里它下面没有 .codegraph/codegraph.db
    g = resolve_graph_adapter(str(tmp_path))
    assert isinstance(g, NullGraphAdapter)


def test_real_adapter_when_db_exists(tmp_path):
    """.codegraph/codegraph.db 存在 → 返回真正的 CodeGraphGraphAdapter（懒打开，不立刻连）。"""
    cg = tmp_path / ".codegraph"     # pathlib.Path / 运算符拼子路径
    cg.mkdir()                       # 建 .codegraph 目录
    (cg / "codegraph.db").write_bytes(b"")  # 建空文件即可（resolve 只看存在性，连接是懒的）
    g = resolve_graph_adapter(str(tmp_path))
    assert isinstance(g, CodeGraphGraphAdapter)
