# tests/test_auth/test_module_of.py
"""module_of：从 CodeGraph file_path 顶层目录取模块；查不到/异常→None。设计 [[模块标签-设计]]。"""
import sqlite3
from src.integrations.codegraph.graph_adapter import CodeGraphGraphAdapter
from src.integrations.codegraph.graph_factory import NullGraphAdapter


class _Node:
    """最小化假 CgNode：module_of 只读 file_path；_resolve 单命中不触发 durable_key。"""
    def __init__(self, file_path):
        self.id = "nid"; self.qualified_name = "X::y"; self.kind = "method"
        self.signature = "()"; self.file_path = file_path


class _FakeDB:
    def __init__(self, file_path=None, raise_err=False):
        self._fp = file_path; self._raise = raise_err
    def find_nodes_by_qualified_name(self, qn):
        if self._raise:
            raise sqlite3.OperationalError("db locked")
        return [_Node(self._fp)] if self._fp is not None else []


def test_module_of_portal():
    adp = CodeGraphGraphAdapter(_FakeDB("mall-portal/src/main/java/com/macro/mall/portal/controller/OmsPortalOrderController.java"))
    assert adp.module_of("OmsPortalOrderController::generateOrder#(OrderParamp)") == "mall-portal"


def test_module_of_admin():
    adp = CodeGraphGraphAdapter(_FakeDB("mall-admin/src/main/java/com/macro/mall/controller/OmsOrderController.java"))
    assert adp.module_of("OmsOrderController::list#()") == "mall-admin"


def test_module_of_unknown_returns_none():
    adp = CodeGraphGraphAdapter(_FakeDB(file_path=None))  # 无命中
    assert adp.module_of("Ghost::x#()") is None


def test_module_of_sqlite_error_returns_none():
    adp = CodeGraphGraphAdapter(_FakeDB(raise_err=True))
    assert adp.module_of("X::y#()") is None


def test_null_adapter_module_of_none():
    assert NullGraphAdapter().module_of("anything::x#()") is None


def test_module_of_node_file_path_none_returns_none():
    """节点存在但 file_path 字段为 None（SQLite 该列 NULL 透传）→ module_of 返 None。

    锁住实现里 `fp = nodes[0].file_path or ""` 的守卫：
    防止将来有人删掉 `or ""` 导致 None.split 抛 AttributeError。
    """
    # 一次性假 DB：有命中（返回 1 个节点），但该节点 file_path 为 None
    class _DBNullFp:
        def find_nodes_by_qualified_name(self, qn):
            return [_Node(None)]  # _resolve 单命中直接返回，不触发 durable_key

    adp = CodeGraphGraphAdapter(_DBNullFp())  # 注入这个假 DB
    assert adp.module_of("X::y#()") is None    # file_path=None → 守卫兜住 → None
