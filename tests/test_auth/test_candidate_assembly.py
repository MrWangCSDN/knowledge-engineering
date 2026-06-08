"""candidate_assembly 模块单测。设计 [[候选按调用顺序组装-设计]] §4.2。

测试覆盖（按 plan 任务推进）：
  - Task 1: TreeNode / CandidateTree dataclass 基本构造
  - Task 2: compute_independent_entries 算法（含真实 mall-swarm RabbitMQ 双路径案例）
  - Task 3: build_subtree_for_entry BFS 子树（候选优先）
  - Task 4: build_candidate_tree 编排（子树 / 孤儿 / token cap / fallback）
  - Task 5: retriever 把 candidate_tree 挂到 ctx 上（wiring 测试）
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


# ───────────────────────────────────────────────────────────────────────────────
# Task 2: compute_independent_entries —— 在候选列表内部识别"真入口"
# 算法核心：双向 reach 集合互查（candidate c 是独立入口 iff ∀d ≠ c, c ∉ reach[d]）
# ───────────────────────────────────────────────────────────────────────────────
from src.service.qa_engine.candidate_assembly import compute_independent_entries


class _FakeGraph:
    """假 GraphProto：用邻接表模拟 successors（不需要 predecessors）。"""
    def __init__(self, succ: dict[str, list[str]]):
        # 字典浅拷贝防外部 mutation 污染
        self._succ = dict(succ)

    def successors(self, qn: str, rel_type=None) -> list[str]:
        # rel_type 兼容 GraphProto 签名；本测试不区分关系类型
        # list(...)：返回新副本，防 mutation；缺失键返 []
        return list(self._succ.get(qn, []))


def test_independent_entries_real_rabbitmq_case():
    """mall-swarm 订单超时取消的真实双路径：generateOrder 和 OrderTimeOutCancelTask 都独立。

    路径 A（定时任务）：OrderTimeOutCancelTask::cancelTimeOutOrder
        → OmsPortalOrderService::cancelTimeOutOrder（接口）
            → OmsPortalOrderServiceImpl::cancelTimeOutOrder（实现）
                → PortalOrderDao::updateOrderStatus
    路径 B（延迟消息）：OmsPortalOrderServiceImpl::generateOrder
        → OmsPortalOrderServiceImpl::sendDelayMessageCancelOrder
            → CancelOrderSender::sendMessage
    """
    g = _FakeGraph({
        "OrderTimeOutCancelTask::cancelTimeOutOrder": ["OmsPortalOrderService::cancelTimeOutOrder"],
        "OmsPortalOrderService::cancelTimeOutOrder": ["OmsPortalOrderServiceImpl::cancelTimeOutOrder"],
        "OmsPortalOrderServiceImpl::cancelTimeOutOrder": ["PortalOrderDao::updateOrderStatus"],
        "OmsPortalOrderServiceImpl::generateOrder": ["OmsPortalOrderServiceImpl::sendDelayMessageCancelOrder"],
        "OmsPortalOrderServiceImpl::sendDelayMessageCancelOrder": ["CancelOrderSender::sendMessage"],
    })
    candidates = [
        "OmsPortalOrderServiceImpl::generateOrder",                  # 独立入口（路径 B 起点）
        "OmsPortalOrderServiceImpl::sendDelayMessageCancelOrder",   # generateOrder 下游 → 排除
        "OrderTimeOutCancelTask::cancelTimeOutOrder",               # 独立入口（路径 A 起点）
        "OmsPortalOrderServiceImpl::cancelTimeOutOrder",            # OrderTimeOutCancelTask 下游 → 排除
        "CancelOrderSender::sendMessage",                            # generateOrder 下游 → 排除
        "PortalOrderDao::updateOrderStatus",                         # OrderTimeOutCancelTask 下游 → 排除
    ]
    independent = compute_independent_entries(candidates, g)
    # 期望：只剩两个真入口，保原序（recall 分高的在前）
    assert independent == [
        "OmsPortalOrderServiceImpl::generateOrder",
        "OrderTimeOutCancelTask::cancelTimeOutOrder",
    ]


def test_independent_entries_single_chain():
    """A → B → C 一条链，三个都在候选 → 只有 A 是独立入口（B/C 都是 A 下游）。"""
    g = _FakeGraph({"A::m": ["B::m"], "B::m": ["C::m"]})
    assert compute_independent_entries(["A::m", "B::m", "C::m"], g) == ["A::m"]


def test_independent_entries_all_disconnected():
    """全部互不调用 → 全部都是独立入口（保原序）。"""
    g = _FakeGraph({})  # 空邻接表 → 任何节点 successors 都是 []
    assert compute_independent_entries(["A::a", "B::b", "C::c"], g) == ["A::a", "B::b", "C::c"]


def test_independent_entries_empty():
    """空候选列表 → 空结果。"""
    assert compute_independent_entries([], _FakeGraph({})) == []


def test_independent_entries_signature_stripping():
    """候选 qn 带 # 签名 + successors 返带签名的 durable_key → 算法应剥签名后比较。

    防边界 bug：CodeGraph durable_key 形如 Cls::m#(p)，与传入候选 qn 形态可能不一致。
    """
    g = _FakeGraph({
        # successors 返带签名的 durable_key（模拟 CodeGraph 真实输出）
        "A::m": ["B::m#(Long)"],
    })
    # 候选用裸 qn（无签名）
    candidates = ["A::m", "B::m"]
    # B::m 是 A::m 下游（即使后者返签名形态），应被识别为非独立入口
    assert compute_independent_entries(candidates, g) == ["A::m"]


# ───────────────────────────────────────────────────────────────────────────────
# Task 3: build_subtree_for_entry —— 从入口 BFS 构造一棵子树，候选优先入子树
# ───────────────────────────────────────────────────────────────────────────────
from src.service.qa_engine.candidate_assembly import build_subtree_for_entry


def test_build_subtree_real_case():
    """OrderTimeOutCancelTask 入口的真实子树：BFS depth=3，候选优先入树、非候选下游不挂。

    场景：图里 ServiceImpl 调 3 个下游（2 个在候选里 + updateIntegration 不在候选里），
    候选优先 → 前 2 个入子树，updateIntegration 跳过。
    """
    g = _FakeGraph({
        "OrderTimeOutCancelTask::cancelTimeOutOrder": ["OmsPortalOrderService::cancelTimeOutOrder"],
        "OmsPortalOrderService::cancelTimeOutOrder": ["OmsPortalOrderServiceImpl::cancelTimeOutOrder"],
        "OmsPortalOrderServiceImpl::cancelTimeOutOrder": [
            "PortalOrderDao::updateOrderStatus",
            "PortalOrderDao::releaseSkuStockLock",
            "UmsMemberService::updateIntegration",                # 不在候选 → 跳过
        ],
    })
    # candidate_meta：qn → {summary, module, code_snippet} 元信息字典
    candidate_meta = {
        "OrderTimeOutCancelTask::cancelTimeOutOrder": {"summary": "定时取消", "module": "mall-portal"},
        "OmsPortalOrderService::cancelTimeOutOrder": {"summary": "接口", "module": "mall-portal"},
        "OmsPortalOrderServiceImpl::cancelTimeOutOrder": {"summary": "实现", "module": "mall-portal"},
        "PortalOrderDao::updateOrderStatus": {"summary": "更新状态", "module": "mall-portal"},
        "PortalOrderDao::releaseSkuStockLock": {"summary": "释放库存", "module": "mall-portal"},
        # updateIntegration 未列入 candidate_meta → 算法不挂进子树
    }
    root = build_subtree_for_entry(
        "OrderTimeOutCancelTask::cancelTimeOutOrder", candidate_meta, g,
    )
    # 根节点验证
    assert root.entity_id == "OrderTimeOutCancelTask::cancelTimeOutOrder"
    assert root.summary == "定时取消"
    assert root.module == "mall-portal"
    # 第一层：接口
    assert len(root.children) == 1
    iface = root.children[0]
    assert iface.entity_id == "OmsPortalOrderService::cancelTimeOutOrder"
    # 第二层：实现
    assert len(iface.children) == 1
    impl = iface.children[0]
    assert impl.entity_id == "OmsPortalOrderServiceImpl::cancelTimeOutOrder"
    # 第三层（depth=3 边界）：候选成员 updateOrderStatus + releaseSkuStockLock 进；
    # 非候选 updateIntegration 不进
    leaf_ids = {c.entity_id for c in impl.children}
    assert leaf_ids == {
        "PortalOrderDao::updateOrderStatus",
        "PortalOrderDao::releaseSkuStockLock",
    }
    # 总节点数 = 1 (root) + 1 (iface) + 1 (impl) + 2 (leaves) = 5
    # 默认 max_nodes_per_subtree=6 → 不触发截断


def test_build_subtree_respects_node_cap():
    """节点上限 max_nodes_per_subtree=6：子树规模超过即截断（首见优先入队）。"""
    # 入口下挂 10 个直接子节点，全部在候选里
    g = _FakeGraph({"Root::m": [f"Leaf::m{i}" for i in range(10)]})
    # 字典字面量合并：Python 3.9+ 的 dict | dict 运算符（PEP 584）
    candidate_meta = {"Root::m": {"summary": "root"}} | {
        f"Leaf::m{i}": {"summary": f"leaf {i}"} for i in range(10)
    }
    root = build_subtree_for_entry("Root::m", candidate_meta, g, max_nodes_per_subtree=6)
    # 1 (root) + 5 (前 5 个叶子) = 6 → 第 6 个叶子开始被截断
    assert len(root.children) == 5


def test_build_subtree_null_graph_returns_root_only():
    """图后端缺失（successors 抛错）→ 只有根节点、无子树（fail-soft）。"""
    class _BrokenGraph:
        def successors(self, qn, rel_type=None):
            # 模拟 sqlite 异常 / NullGraph 抛错
            raise RuntimeError("graph backend down")
    candidate_meta = {"A::m": {"summary": "A 解读", "module": None}}
    root = build_subtree_for_entry("A::m", candidate_meta, _BrokenGraph(), max_nodes_per_subtree=6)
    # 根仍正确构造（不依赖 successors）；children 因 successors 异常 → 空
    assert root.entity_id == "A::m"
    assert root.summary == "A 解读"
    assert root.children == []


def test_build_subtree_skips_non_candidate_descendants():
    """子树严格只挂候选成员；非候选的下游 entity 即使在图里也不进。

    防止 BFS 子树爆出候选边界，导致树形渲染包含未召回过的 entity 引诱 LLM 引用。
    """
    g = _FakeGraph({"A::m": ["X::stranger", "B::m"], "B::m": ["Y::stranger"]})
    candidate_meta = {"A::m": {"summary": "A"}, "B::m": {"summary": "B"}}  # X / Y 不在候选
    root = build_subtree_for_entry("A::m", candidate_meta, g)
    # 只挂 B::m，X::stranger 跳过
    assert len(root.children) == 1
    assert root.children[0].entity_id == "B::m"
    # B 的下游 Y 不在候选 → 不挂
    assert root.children[0].children == []
