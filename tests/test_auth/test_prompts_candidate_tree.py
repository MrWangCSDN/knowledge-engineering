"""prompt 候选区树形渲染不变量测试。设计 [[候选按调用顺序组装-设计]] §4.3。

不验真实 LLM 输出，验 prompt 文本结构 —— 多入口 → 树形分组标志 / 单入口 → 扁平 /
孤儿附录 / 桥接 note 都按预期出现在 prompt 字符串里。
"""
from src.service.qa_engine.candidate_assembly import TreeNode, CandidateTree
from src.service.qa_engine.prompts import build_user_prompt


def _tree_ctx(tree: CandidateTree, candidates: list[dict]) -> dict:
    """构造 build_user_prompt 用的最小 ctx dict。"""
    return {
        "entry_candidates": candidates,           # 扁平兜底用
        "candidate_tree": tree,                   # 树形分支判定用
        "candidate_code_snippets": {},
        "callees_by_entry": {},
        "callers_by_entry": {},
        "call_edges_by_entry": {},
        "table_access_by_entry": {},
        "skill_id": "architecture",
    }


def test_multi_entry_renders_subtree_headers():
    """两棵子树 → prompt 含【子树 1】【子树 2】标题 + 缩进 ├─ / └─ 树形符号 + note。"""
    sub1 = TreeNode(
        entity_id="Cls1::m", summary="下单", module="mall-portal", code_snippet=None,
        children=[
            TreeNode(
                entity_id="Cls1::sub", summary="下游", module=None,
                code_snippet=None, children=[],
            ),
        ],
    )
    sub2 = TreeNode(
        entity_id="Task::scan", summary="定时", module="mall-portal",
        code_snippet=None, children=[],
    )
    tree = CandidateTree(
        subtrees=[sub1, sub2],
        orphans=[],
        fallback_to_flat=False,
        notes=["多入口检测：识别到 2 个独立业务路径"],
    )
    candidates = [
        {"entity_id": "Cls1::m", "summary_text": "下单"},
        {"entity_id": "Task::scan", "summary_text": "定时"},
    ]
    prompt = build_user_prompt("订单怎么取消", _tree_ctx(tree, candidates))
    # 子树标题
    assert "【子树 1】" in prompt
    assert "【子树 2】" in prompt
    # 树形符号（至少一个出现；单子节点用 └─）
    assert "└─" in prompt or "├─" in prompt
    # note 进 prompt（让 LLM 知道多入口、不要编跨边）
    assert "多入口" in prompt


def test_single_entry_uses_flat():
    """单棵子树 → 走扁平分支（保旧行为，文案"按相关度倒序"沿用）。"""
    sub1 = TreeNode(
        entity_id="Cls::m", summary="一", module=None,
        code_snippet=None, children=[],
    )
    tree = CandidateTree(subtrees=[sub1], orphans=[], fallback_to_flat=False, notes=[])
    candidates = [{"entity_id": "Cls::m", "summary_text": "一"}]
    prompt = build_user_prompt("问题", _tree_ctx(tree, candidates))
    # 扁平分支 → 不含子树标题，沿用原文案
    assert "【子树 1】" not in prompt
    assert "按相关度倒序" in prompt


def test_orphan_appendix_rendered():
    """孤儿 → prompt 含【其他相关实体（未连入主路径）】段 + 孤儿 entity_id。"""
    sub1 = TreeNode(entity_id="A::m", summary="A", module=None, code_snippet=None, children=[])
    sub2 = TreeNode(entity_id="B::m", summary="B", module=None, code_snippet=None, children=[])
    orphan = TreeNode(
        entity_id="CancelOrderReceiver::handle", summary="MQ 监听器",
        module="mall-portal", code_snippet=None, children=[],
    )
    tree = CandidateTree(
        subtrees=[sub1, sub2], orphans=[orphan],
        fallback_to_flat=False, notes=[],
    )
    candidates = [
        {"entity_id": "A::m"}, {"entity_id": "B::m"},
        {"entity_id": "CancelOrderReceiver::handle"},
    ]
    prompt = build_user_prompt("Q", _tree_ctx(tree, candidates))
    assert "其他相关实体" in prompt
    assert "CancelOrderReceiver::handle" in prompt


def test_fallback_to_flat_when_tree_none():
    """ctx.candidate_tree=None → 走扁平（向后兼容老调用方）。"""
    candidates = [{"entity_id": "A::m", "summary_text": "A"}]
    ctx = _tree_ctx(
        CandidateTree(subtrees=[], orphans=[], fallback_to_flat=False, notes=[]),
        candidates,
    )
    ctx["candidate_tree"] = None                  # 显式置 None
    prompt = build_user_prompt("Q", ctx)
    assert "按相关度倒序" in prompt
    assert "【子树" not in prompt


def test_fallback_to_flat_when_tree_signals():
    """tree.fallback_to_flat=True → 走扁平（token 超阈值兜底）。"""
    sub1 = TreeNode(
        entity_id="A::m", summary="A", module=None, code_snippet=None,
        children=[TreeNode(
            entity_id="B::m", summary="B", module=None,
            code_snippet=None, children=[],
        )],
    )
    sub2 = TreeNode(entity_id="C::m", summary="C", module=None, code_snippet=None, children=[])
    tree = CandidateTree(subtrees=[sub1, sub2], orphans=[], fallback_to_flat=True, notes=[])
    candidates = [
        {"entity_id": "A::m"}, {"entity_id": "B::m"}, {"entity_id": "C::m"},
    ]
    prompt = build_user_prompt("Q", _tree_ctx(tree, candidates))
    assert "按相关度倒序" in prompt
    assert "【子树" not in prompt
