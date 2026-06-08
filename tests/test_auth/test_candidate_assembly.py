"""candidate_assembly 模块单测。设计 [[候选按调用顺序组装-设计]] §4.2。

测试覆盖（按 plan 任务推进）：
  - Task 1: TreeNode / CandidateTree dataclass 基本构造
  - Task 2: compute_independent_entries 算法（含真实 mall-swarm RabbitMQ 双路径案例）
  - Task 3: build_subtree_for_entry BFS 子树构造（候选优先 + 节点上限 + 异常降级）
  - Task 4: build_candidate_tree 编排（多入口 / 孤儿 / token cap / fallback）
  - Task 5: retriever 端到端 wiring
"""
# 被测产物（按需追加，避免一次性 import 未实现符号导致全文件 ImportError）
from src.service.qa_engine.candidate_assembly import TreeNode, CandidateTree


def test_treenode_basic_construction():
    """TreeNode 可构造 + children 默认空 list。"""
    # 构造一个最简节点：entity_id + summary + 模块 + 无源码 + 无子节点
    node = TreeNode(
        entity_id="Cls::m",
        summary="某方法",
        module="mall-portal",
        code_snippet=None,
        children=[],
    )
    # 字段访问检查
    assert node.entity_id == "Cls::m"
    assert node.summary == "某方法"
    assert node.module == "mall-portal"
    assert node.code_snippet is None
    assert node.children == []


def test_candidate_tree_empty_construction():
    """CandidateTree 是 dataclass，含 subtrees / orphans / fallback_to_flat / notes 四字段。"""
    # 空构造：所有列表为 [], fallback 为 False
    tree = CandidateTree(subtrees=[], orphans=[], fallback_to_flat=False, notes=[])
    assert tree.subtrees == []
    assert tree.orphans == []
    assert tree.fallback_to_flat is False
    assert tree.notes == []
