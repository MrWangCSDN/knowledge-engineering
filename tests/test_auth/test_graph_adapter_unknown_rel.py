"""验证未知 rel_type（如 accesses_table）不再被误当 'calls'，而是返回 []。

修 Phase 1 引入的 bug：_REL_TO_KIND.get(rel_type, 'calls') 把未知 rel_type fallback 到 'calls'，
导致 _extract_table_access / ke_table_access 查 accesses_table 时拿回 callees、误当成表名。
CodeGraph 根本没有 accesses_table 边，正确行为是返回 []（对齐旧 Neo4j「查不到该边返空」契约）。
"""
from src.integrations.codegraph.graph_adapter import CodeGraphGraphAdapter


class _Node:
    """最小化的假 CgNode：只带 durable_key + successors/predecessors 用到的字段。"""

    def __init__(self, nid, qn, kind="method", signature="()"):
        self.id = nid                  # successors/predecessors 用 node.id 查
        self.qualified_name = qn       # durable_key 用
        self.kind = kind               # durable_key 用（method 才拼签名）
        self.signature = signature     # durable_key 用


class _FakeDB:
    """假 DB：只有 kind=='calls' 的边有邻居；其它 kind（含 accesses_table）一律无邻居。

    这样若 adapter 把未知 rel_type fallback 到 'calls'，就会错误地返回 callees → 测试失败暴露 bug。
    """

    def find_nodes_by_qualified_name(self, qn):
        return [_Node("nid:" + qn, qn)]            # 唯一候选（_resolve 直接返回）

    def successors(self, node_id, kind="calls"):
        return [_Node("callee", "A::callee")] if kind == "calls" else []

    def predecessors(self, node_id, kind="calls"):
        return [_Node("caller", "A::caller")] if kind == "calls" else []


def test_unknown_rel_type_returns_empty():
    """accesses_table 等 CodeGraph 没有的边 → 必须返回 []，不能 fallback 到 calls。"""
    adp = CodeGraphGraphAdapter(_FakeDB())
    assert adp.successors("X::y", rel_type="accesses_table") == []
    assert adp.predecessors("X::y", rel_type="accesses_table") == []


def test_calls_rel_type_still_works():
    """calls / None（默认）仍正常返回邻居，不被本次修复误伤。"""
    adp = CodeGraphGraphAdapter(_FakeDB())
    assert adp.successors("X::y", rel_type="calls") != []
    assert adp.successors("X::y", rel_type=None) != []   # None 等效 calls
    assert adp.predecessors("X::y") != []
