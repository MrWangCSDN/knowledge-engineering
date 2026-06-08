# 候选按调用顺序组装 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans。Steps 用 checkbox 跟踪。Phase-0 已完成（用户实测 mall-swarm "订单超时" 案例已验证两条独立路径假设）。

**Goal:** QA prompt 候选区从扁平 1-10 清单升级为按调用子树分组（动态独立入口 + BFS 子树 + 孤儿附录），治 LLM 在并行业务路径上的跨路径错连。

**Architecture:** 新增独立纯函数模块 `candidate_assembly.py`，retriever 计算后挂在 ctx 上、prompts 端读出渲染。算法核心：`compute_independent_entries`（在 candidates 内部用下游 reachability 集合互查）+ `build_subtree_for_entry`（BFS 优先候选）+ `build_candidate_tree`（多入口编排 + 孤儿 + token 估算 + 降扁平兜底）。

**Tech Stack:** Python 3.12 + dataclass + GraphProto（CodeGraph 适配器）+ pytest。复用现有 retriever / prompts.build_user_prompt 集成点；前端不动。

**约束:** TDD bite-sized；Python 中文逐行注释；frequent commits；设计文档 [[候选按调用顺序组装-设计]] Obsidian 不双写；部署 git bundle → ke-api restart 需授权。**不动** 召回 / call_edges_by_entry / render_call_graph / 6 段 / 前端。

---

## 文件结构

**新建：**
- `src/service/qa_engine/candidate_assembly.py` — `TreeNode` / `CandidateTree` dataclass + `compute_independent_entries` / `build_subtree_for_entry` / `build_candidate_tree` 纯函数
- `tests/test_auth/test_candidate_assembly.py` — 算法单测（独立入口识别 / BFS 子树 / 孤儿 / token cap / fallback / NullGraph 降级）
- `tests/test_auth/test_prompts_candidate_tree.py` — prompt 结构不变量（多入口 → 子树标志 / 单入口 → 扁平 / 孤儿附录 / 桥接 note）

**修改：**
- `src/service/qa_engine/retriever.py` — 计算 `ctx.candidate_tree`（新字段）
- `src/service/qa_engine/synthesizer.py:_ctx_to_dict` — 把 `candidate_tree` 透传给 prompt builder
- `src/service/qa_engine/prompts.py:build_user_prompt` — 候选区加 tree 分支，提取 `_render_flat_candidates` 公共 helper（保旧行为不变）

**前端：** 零改动。

---

### Task 1: candidate_assembly 模块骨架 + TreeNode/CandidateTree dataclass

**Files:**
- Create: `src/service/qa_engine/candidate_assembly.py`
- Test: `tests/test_auth/test_candidate_assembly.py`

- [ ] **Step 1: 写失败测试（红）**

```python
# tests/test_auth/test_candidate_assembly.py
"""candidate_assembly 模块单测。设计 [[候选按调用顺序组装-设计]] §4.2。"""
from src.service.qa_engine.candidate_assembly import TreeNode, CandidateTree


def test_treenode_basic_construction():
    """TreeNode 是 frozen dataclass，可构造 + children 是 list。"""
    node = TreeNode(
        entity_id="Cls::m",
        summary="某方法",
        module="mall-portal",
        code_snippet=None,
        children=[],
    )
    assert node.entity_id == "Cls::m"
    assert node.summary == "某方法"
    assert node.children == []


def test_candidate_tree_empty_construction():
    """CandidateTree 是 dataclass，含 subtrees / orphans / fallback_to_flat / notes 四字段。"""
    tree = CandidateTree(subtrees=[], orphans=[], fallback_to_flat=False, notes=[])
    assert tree.subtrees == []
    assert tree.orphans == []
    assert tree.fallback_to_flat is False
    assert tree.notes == []
```

- [ ] **Step 2: 跑红** — `cd /Users/java/knowledge-engineering-auth && venv/bin/python -m pytest tests/test_auth/test_candidate_assembly.py -x`（期：ImportError TreeNode）

- [ ] **Step 3: 实现 dataclass**

```python
# src/service/qa_engine/candidate_assembly.py
"""候选按调用顺序组装：把召回扁平 candidates 重组为按业务子树分组的 prompt 表示。

设计 [[候选按调用顺序组装-设计]] §4.2。核心：
  - compute_independent_entries：从候选里挑"不被其他候选 BFS 下游包含"的真正独立入口
  - build_subtree_for_entry：从入口 BFS depth=3，优先候选成员入子树
  - build_candidate_tree：编排——多子树 + 孤儿 + token 估算 + 降扁平兜底
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TreeNode:
    """候选树的一个节点。children 是 list[TreeNode]；构造时显式传 children=[]。

    设计 §4.2：节点字段对齐 prompt 渲染所需。frozen=False 让构造期可以 append children
    （子树构造算法用递归填子节点）；构造完返出后调用方应只读。
    """
    entity_id: str                            # 候选 qualified_name（不含 # 签名）
    summary: str                              # 业务说明（已截断到上限）
    module: Optional[str]                     # 模块名（如 mall-portal）；None 表示未知
    code_snippet: Optional[str]               # source-first grounding P1 命中的真实源码；None 表示无
    children: list["TreeNode"] = field(default_factory=list)  # 子节点


@dataclass
class CandidateTree:
    """build_candidate_tree 的产出。"""
    subtrees: list[TreeNode]                  # 独立入口对应的子树（最多 max_entries 棵）
    orphans: list[TreeNode]                   # 孤儿候选（top-N，无 children）
    fallback_to_flat: bool                    # token 估算超阈值 → 调用方应降级扁平
    notes: list[str]                          # 元信息（如"多入口检测：可能 MQ 异步桥接"）
```

- [ ] **Step 4: 跑绿** — `venv/bin/python -m pytest tests/test_auth/test_candidate_assembly.py -x`（期：2 passed）

- [ ] **Step 5: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/qa_engine/candidate_assembly.py tests/test_auth/test_candidate_assembly.py
git commit -m "feat(qa): candidate_assembly 模块骨架 + TreeNode/CandidateTree dataclass (Task 1)"
```

---

### Task 2: compute_independent_entries 算法

**Files:**
- Modify: `src/service/qa_engine/candidate_assembly.py`（追加）
- Modify: `tests/test_auth/test_candidate_assembly.py`（追加 4 测试 + _FakeGraph）

- [ ] **Step 1: 加 _FakeGraph + 4 失败测试（红）**

```python
# tests/test_auth/test_candidate_assembly.py 追加
from src.service.qa_engine.candidate_assembly import compute_independent_entries


class _FakeGraph:
    """假 GraphProto：用邻接表模拟 successors（不需要 predecessors）。"""
    def __init__(self, succ: dict[str, list[str]]):
        self._succ = succ
    def successors(self, qn: str, rel_type=None) -> list[str]:
        # qn 可能带签名，剥掉对比；本测试都用裸 qn
        return list(self._succ.get(qn, []))


def test_independent_entries_real_rabbitmq_case():
    """mall-swarm 订单超时取消的真实双路径：generateOrder 和 OrderTimeOutCancelTask 都独立。"""
    # 路径 A（定时）：OrderTimeOutCancelTask → OmsPortalOrderService::cancelTimeOutOrder → Impl
    # 路径 B（消息）：generateOrder → sendDelayMessageCancelOrder → CancelOrderSender::sendMessage
    g = _FakeGraph({
        "OrderTimeOutCancelTask::cancelTimeOutOrder": ["OmsPortalOrderService::cancelTimeOutOrder"],
        "OmsPortalOrderService::cancelTimeOutOrder": ["OmsPortalOrderServiceImpl::cancelTimeOutOrder"],
        "OmsPortalOrderServiceImpl::cancelTimeOutOrder": ["PortalOrderDao::updateOrderStatus"],
        "OmsPortalOrderServiceImpl::generateOrder": ["OmsPortalOrderServiceImpl::sendDelayMessageCancelOrder"],
        "OmsPortalOrderServiceImpl::sendDelayMessageCancelOrder": ["CancelOrderSender::sendMessage"],
    })
    candidates = [
        "OmsPortalOrderServiceImpl::generateOrder",
        "OmsPortalOrderServiceImpl::sendDelayMessageCancelOrder",   # 是 generateOrder 下游 → 排除
        "OrderTimeOutCancelTask::cancelTimeOutOrder",
        "OmsPortalOrderServiceImpl::cancelTimeOutOrder",            # 是 OrderTimeOutCancelTask 下游 → 排除
        "CancelOrderSender::sendMessage",                            # 是 generateOrder 下游 → 排除
        "PortalOrderDao::updateOrderStatus",                         # 是 OrderTimeOutCancelTask 下游 → 排除
    ]
    independent = compute_independent_entries(candidates, g)
    # 期望：只剩两个真入口，保原序
    assert independent == [
        "OmsPortalOrderServiceImpl::generateOrder",
        "OrderTimeOutCancelTask::cancelTimeOutOrder",
    ]


def test_independent_entries_single_chain():
    """一条链：A → B → C，三个都在候选 → 只有 A 是独立入口。"""
    g = _FakeGraph({"A::m": ["B::m"], "B::m": ["C::m"]})
    assert compute_independent_entries(["A::m", "B::m", "C::m"], g) == ["A::m"]


def test_independent_entries_all_disconnected():
    """全部互不调用 → 全部都是独立入口（保原序）。"""
    g = _FakeGraph({})
    assert compute_independent_entries(["A::a", "B::b", "C::c"], g) == ["A::a", "B::b", "C::c"]


def test_independent_entries_empty():
    """空候选列表 → 空结果。"""
    assert compute_independent_entries([], _FakeGraph({})) == []
```

- [ ] **Step 2: 跑红** — `venv/bin/python -m pytest tests/test_auth/test_candidate_assembly.py -x`（期：ImportError compute_independent_entries）

- [ ] **Step 3: 实现算法**

```python
# src/service/qa_engine/candidate_assembly.py 追加
from collections import deque


def _strip_signature(durable_key: str) -> str:
    """剥 # 签名（CodeGraph durable_key 可能形如 Cls::m#(p)），便于 qn 比较。"""
    return durable_key.split("#", 1)[0]


def _bfs_reachable(start: str, graph, max_depth: int) -> set[str]:
    """BFS 收集 start 下游可达节点的 qn 集合（不含 start 自身）。

    Args:
        start: 起始 qualified_name（已剥签名）
        graph: 实现 successors(qn) → list[durable_key] 的对象
        max_depth: 最大 BFS 深度（深度 0 = 起点，1 = 直接下游）
    Returns:
        所有可达 qn 的集合
    """
    reachable: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(start, 0)])
    while queue:
        node, depth = queue.popleft()
        if depth >= max_depth:
            continue
        try:
            succs = graph.successors(node) or []
        except Exception:
            # 图后端异常 / NullGraph 返 [] → 该节点无下游，继续别的
            succs = []
        for s in succs:
            qn = _strip_signature(s)
            if qn != start and qn not in reachable:
                reachable.add(qn)
                queue.append((qn, depth + 1))
    return reachable


def compute_independent_entries(
    candidate_qns: list[str],
    graph,
    *,
    max_depth: int = 3,
) -> list[str]:
    """从候选 qn 列表识别独立入口：不在任何其他候选下游 BFS 可达集里的。

    保序：按 candidate_qns 原顺序输出，让首入口为召回分最高的。

    Args:
        candidate_qns: 候选 qn 列表（已剥签名；retriever 调用方应预处理）
        graph: GraphProto-like，提供 successors
        max_depth: BFS 深度上限（默认 3）
    Returns:
        独立入口 qn 列表（原序的子集）
    """
    # 1. 每个候选预算 BFS 下游可达集合
    reach: dict[str, set[str]] = {
        c: _bfs_reachable(c, graph, max_depth) for c in candidate_qns
    }
    # 2. 独立判定：c ∈ candidates 且 ∀d ≠ c, c ∉ reach[d]
    independent: list[str] = []
    for c in candidate_qns:
        is_descendant = False
        for d in candidate_qns:
            if d == c:
                continue
            if c in reach[d]:
                is_descendant = True
                break
        if not is_descendant:
            independent.append(c)
    return independent
```

- [ ] **Step 4: 跑绿** — `venv/bin/python -m pytest tests/test_auth/test_candidate_assembly.py -x`（期：6 passed，含 Task 1 的 2 个）

- [ ] **Step 5: Commit**

```bash
git add src/service/qa_engine/candidate_assembly.py tests/test_auth/test_candidate_assembly.py
git commit -m "feat(qa): compute_independent_entries 算法 — 双向 reach 集合互查（Task 2）"
```

---

### Task 3: build_subtree_for_entry — 从入口 BFS 建子树

**Files:**
- Modify: `src/service/qa_engine/candidate_assembly.py`
- Modify: `tests/test_auth/test_candidate_assembly.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_auth/test_candidate_assembly.py 追加
from src.service.qa_engine.candidate_assembly import build_subtree_for_entry


def test_build_subtree_real_case():
    """OrderTimeOutCancelTask 入口的子树：BFS depth=3，候选优先入树。"""
    g = _FakeGraph({
        "OrderTimeOutCancelTask::cancelTimeOutOrder": ["OmsPortalOrderService::cancelTimeOutOrder"],
        "OmsPortalOrderService::cancelTimeOutOrder": ["OmsPortalOrderServiceImpl::cancelTimeOutOrder"],
        "OmsPortalOrderServiceImpl::cancelTimeOutOrder": [
            "PortalOrderDao::updateOrderStatus",
            "PortalOrderDao::releaseSkuStockLock",
            "UmsMemberService::updateIntegration",
        ],
    })
    candidate_meta = {
        "OrderTimeOutCancelTask::cancelTimeOutOrder": {"summary": "定时取消", "module": "mall-portal"},
        "OmsPortalOrderService::cancelTimeOutOrder": {"summary": "接口", "module": "mall-portal"},
        "OmsPortalOrderServiceImpl::cancelTimeOutOrder": {"summary": "实现", "module": "mall-portal"},
        "PortalOrderDao::updateOrderStatus": {"summary": "更新状态", "module": "mall-portal"},
        "PortalOrderDao::releaseSkuStockLock": {"summary": "释放库存", "module": "mall-portal"},
        # updateIntegration 不在候选 → 不挂进子树（候选优先）
    }
    root = build_subtree_for_entry(
        "OrderTimeOutCancelTask::cancelTimeOutOrder", candidate_meta, g,
    )
    # 根
    assert root.entity_id == "OrderTimeOutCancelTask::cancelTimeOutOrder"
    assert root.summary == "定时取消"
    # 第一层：接口
    assert len(root.children) == 1
    iface = root.children[0]
    assert iface.entity_id == "OmsPortalOrderService::cancelTimeOutOrder"
    # 第二层：实现
    impl = iface.children[0]
    assert impl.entity_id == "OmsPortalOrderServiceImpl::cancelTimeOutOrder"
    # 第三层（depth=3 边界）：候选优先 → updateOrderStatus + releaseSkuStockLock 进；updateIntegration 不进
    leaf_ids = {c.entity_id for c in impl.children}
    assert "PortalOrderDao::updateOrderStatus" in leaf_ids
    assert "PortalOrderDao::releaseSkuStockLock" in leaf_ids
    assert "UmsMemberService::updateIntegration" not in leaf_ids  # 非候选 → 不入


def test_build_subtree_respects_node_cap():
    """节点上限 max_nodes_per_subtree=6：子树超过即截断。"""
    # 入口下挂 10 个直接子节点
    g = _FakeGraph({"Root::m": [f"Leaf::m{i}" for i in range(10)]})
    candidate_meta = {"Root::m": {"summary": "root"}} | {
        f"Leaf::m{i}": {"summary": f"leaf {i}"} for i in range(10)
    }
    root = build_subtree_for_entry("Root::m", candidate_meta, g, max_nodes_per_subtree=6)
    # 1（root）+ 5（叶子）= 6
    assert len(root.children) == 5


def test_build_subtree_null_graph_returns_root_only():
    """图后端缺失（successors 抛错）→ 只有根节点，无子树。"""
    class _Broken:
        def successors(self, qn): raise RuntimeError("graph down")
    candidate_meta = {"A::m": {"summary": "A 解读"}}
    root = build_subtree_for_entry("A::m", candidate_meta, _Broken(), max_nodes_per_subtree=6)
    assert root.entity_id == "A::m"
    assert root.summary == "A 解读"
    assert root.children == []
```

- [ ] **Step 2: 跑红** — `venv/bin/python -m pytest tests/test_auth/test_candidate_assembly.py -x`（期：ImportError build_subtree_for_entry）

- [ ] **Step 3: 实现 BFS 子树**

```python
# src/service/qa_engine/candidate_assembly.py 追加
def build_subtree_for_entry(
    entry_qn: str,
    candidate_meta: dict[str, dict],
    graph,
    *,
    max_depth: int = 3,
    max_nodes_per_subtree: int = 6,
) -> TreeNode:
    """从 entry BFS 构造一棵候选子树。

    优先级：子节点 in candidate_meta 优先入子树（让候选成员先占名额）。
    截断：节点数达 max_nodes_per_subtree 即停止扩展，避免单子树爆炸。

    Args:
        entry_qn: 入口 qualified_name（已剥签名）
        candidate_meta: {qn: {summary, module?, code_snippet?}}；只这些会被挂进子树
        graph: GraphProto-like
        max_depth: BFS 深度（默认 3）
        max_nodes_per_subtree: 整棵子树节点上限（含根；默认 6）
    Returns:
        子树根 TreeNode
    """
    candidate_qns_set: set[str] = set(candidate_meta.keys())

    def _make_node(qn: str) -> TreeNode:
        """根据 qn 构造 TreeNode；从 candidate_meta 取元信息，缺则默认。"""
        meta = candidate_meta.get(qn, {})
        return TreeNode(
            entity_id=qn,
            summary=meta.get("summary") or "",
            module=meta.get("module"),
            code_snippet=meta.get("code_snippet"),
            children=[],
        )

    root = _make_node(entry_qn)
    visited: set[str] = {entry_qn}
    node_lookup: dict[str, TreeNode] = {entry_qn: root}
    queue: deque[tuple[str, int]] = deque([(entry_qn, 0)])
    nodes_added = 1  # 已含 root

    while queue and nodes_added < max_nodes_per_subtree:
        current_qn, depth = queue.popleft()
        if depth >= max_depth:
            continue
        try:
            succs = graph.successors(current_qn) or []
        except Exception:
            succs = []
        # 候选优先：候选成员排前
        succ_qns = [_strip_signature(s) for s in succs]
        prioritized = sorted(
            succ_qns,
            key=lambda q: (0 if q in candidate_qns_set else 1),
        )
        for succ_qn in prioritized:
            if nodes_added >= max_nodes_per_subtree:
                break
            if succ_qn in visited:
                continue
            # 只挂候选成员（非候选 succ 跳过，不污染子树）
            if succ_qn not in candidate_qns_set:
                continue
            visited.add(succ_qn)
            child = _make_node(succ_qn)
            node_lookup[current_qn].children.append(child)
            node_lookup[succ_qn] = child
            nodes_added += 1
            queue.append((succ_qn, depth + 1))
    return root
```

- [ ] **Step 4: 跑绿** — `venv/bin/python -m pytest tests/test_auth/test_candidate_assembly.py -x`（期：9 passed）

- [ ] **Step 5: Commit**

```bash
git add src/service/qa_engine/candidate_assembly.py tests/test_auth/test_candidate_assembly.py
git commit -m "feat(qa): build_subtree_for_entry — BFS 入口子树（候选优先）（Task 3）"
```

---

### Task 4: build_candidate_tree 主编排 — 子树 + 孤儿 + token cap + fallback

**Files:**
- Modify: `src/service/qa_engine/candidate_assembly.py`
- Modify: `tests/test_auth/test_candidate_assembly.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_auth/test_candidate_assembly.py 追加
from src.service.qa_engine.candidate_assembly import build_candidate_tree


def test_build_candidate_tree_multi_entry_with_orphan():
    """双路径 + 一个孤儿：subtrees 长 2，orphans 长 1，notes 含"多入口"提示。"""
    g = _FakeGraph({
        "OrderTimeOutCancelTask::cancelTimeOutOrder": ["OmsPortalOrderService::cancelTimeOutOrder"],
        "OmsPortalOrderService::cancelTimeOutOrder": ["OmsPortalOrderServiceImpl::cancelTimeOutOrder"],
        "OmsPortalOrderServiceImpl::generateOrder": ["OmsPortalOrderServiceImpl::sendDelayMessageCancelOrder"],
    })
    candidates = [
        {"entity_id": "OmsPortalOrderServiceImpl::generateOrder", "summary_text": "用户下单", "module": "mall-portal", "score": 0.78},
        {"entity_id": "OmsPortalOrderServiceImpl::sendDelayMessageCancelOrder", "summary_text": "预约取消", "module": "mall-portal", "score": 0.76},
        {"entity_id": "OrderTimeOutCancelTask::cancelTimeOutOrder", "summary_text": "定时扫描", "module": "mall-portal", "score": 0.74},
        {"entity_id": "OmsPortalOrderService::cancelTimeOutOrder", "summary_text": "接口", "module": "mall-portal", "score": 0.72},
        {"entity_id": "OmsPortalOrderServiceImpl::cancelTimeOutOrder", "summary_text": "实现", "module": "mall-portal", "score": 0.70},
        {"entity_id": "CancelOrderReceiver::handle", "summary_text": "MQ 监听器", "module": "mall-portal", "score": 0.65},  # 孤儿
    ]
    tree = build_candidate_tree(candidates, code_snippets={}, graph=g)
    # 两棵子树（generateOrder + OrderTimeOutCancelTask）
    assert len(tree.subtrees) == 2
    entries = {st.entity_id for st in tree.subtrees}
    assert "OmsPortalOrderServiceImpl::generateOrder" in entries
    assert "OrderTimeOutCancelTask::cancelTimeOutOrder" in entries
    # 一个孤儿（CancelOrderReceiver::handle）
    assert len(tree.orphans) == 1
    assert tree.orphans[0].entity_id == "CancelOrderReceiver::handle"
    # 多入口 → notes 含跨边界提示
    assert any("多入口" in n or "异步" in n or "桥接" in n for n in tree.notes)
    # 不触发 fallback
    assert tree.fallback_to_flat is False


def test_build_candidate_tree_caps_entries_at_3():
    """4 个独立入口 → 只保前 3 棵子树（按 recall 顺序，第 4 个进孤儿）。"""
    g = _FakeGraph({})  # 全部互不调用 → 4 个都独立
    candidates = [
        {"entity_id": "E1::m", "summary_text": "1", "score": 0.9},
        {"entity_id": "E2::m", "summary_text": "2", "score": 0.85},
        {"entity_id": "E3::m", "summary_text": "3", "score": 0.8},
        {"entity_id": "E4::m", "summary_text": "4", "score": 0.75},
    ]
    tree = build_candidate_tree(candidates, code_snippets={}, graph=g, max_entries=3)
    assert len(tree.subtrees) == 3
    assert [st.entity_id for st in tree.subtrees] == ["E1::m", "E2::m", "E3::m"]
    assert [o.entity_id for o in tree.orphans] == ["E4::m"]


def test_build_candidate_tree_caps_orphans():
    """孤儿超 max_orphans=3 → 截断。"""
    g = _FakeGraph({})
    candidates = [{"entity_id": f"E{i}::m", "summary_text": str(i), "score": 1.0 - i * 0.01} for i in range(8)]
    tree = build_candidate_tree(candidates, code_snippets={}, graph=g, max_entries=3, max_orphans=3)
    assert len(tree.subtrees) == 3
    assert len(tree.orphans) == 3


def test_build_candidate_tree_single_entry_no_notes():
    """单入口（无并行路径）→ notes 不强调"多入口/异步"（避免误导 LLM）。"""
    g = _FakeGraph({"A::m": ["B::m"]})
    candidates = [
        {"entity_id": "A::m", "summary_text": "入口", "score": 0.9},
        {"entity_id": "B::m", "summary_text": "下游", "score": 0.85},
    ]
    tree = build_candidate_tree(candidates, code_snippets={}, graph=g)
    assert len(tree.subtrees) == 1
    # notes 不含"多入口"等关键词（单入口）
    assert not any("多入口" in n for n in tree.notes)


def test_build_candidate_tree_token_cap_triggers_fallback():
    """所有 summary 拼起来超 token_safety_cap → fallback_to_flat=True。"""
    g = _FakeGraph({})
    # 8 个独立候选，每个 summary 4000 字符 → 总 ~32000 字符 / 3.5 ≈ 9000 token > 8000
    big_summary = "业" * 4000
    candidates = [
        {"entity_id": f"E{i}::m", "summary_text": big_summary, "score": 1.0 - i * 0.01}
        for i in range(8)
    ]
    tree = build_candidate_tree(candidates, code_snippets={}, graph=g, token_safety_cap=8000)
    assert tree.fallback_to_flat is True


def test_build_candidate_tree_empty_candidates():
    """空候选 → 空 tree（subtrees/orphans 都空，不 fallback）。"""
    tree = build_candidate_tree([], code_snippets={}, graph=_FakeGraph({}))
    assert tree.subtrees == []
    assert tree.orphans == []
    assert tree.fallback_to_flat is False
```

- [ ] **Step 2: 跑红** — `venv/bin/python -m pytest tests/test_auth/test_candidate_assembly.py -x`（期：ImportError build_candidate_tree）

- [ ] **Step 3: 实现编排**

```python
# src/service/qa_engine/candidate_assembly.py 追加
def _estimate_tokens_in_tree(tree: CandidateTree) -> int:
    """粗算 tree 渲染所需 token：char 总数 / 3.5（中英混合估算）。"""
    chars = 0
    def _count(node: TreeNode) -> None:
        nonlocal chars
        chars += len(node.entity_id) + len(node.summary)
        if node.code_snippet:
            chars += len(node.code_snippet)
        for c in node.children:
            _count(c)
    for st in tree.subtrees:
        _count(st)
    for o in tree.orphans:
        _count(o)
    return max(1, int(chars / 3.5))


def _collect_all_tree_qns(node: TreeNode) -> set[str]:
    """递归收集子树里所有节点 qn（含 root）。"""
    out: set[str] = {node.entity_id}
    for c in node.children:
        out.update(_collect_all_tree_qns(c))
    return out


def build_candidate_tree(
    candidates: list[dict],
    code_snippets: dict[str, str],
    graph,
    *,
    max_entries: int = 3,
    max_depth: int = 3,
    max_nodes_per_subtree: int = 6,
    max_orphans: int = 3,
    max_summary_chars: int = 300,
    token_safety_cap: int = 8000,
) -> CandidateTree:
    """候选树编排主函数。

    流程：
      1. 候选 qn 提取（剥签名）
      2. compute_independent_entries → 取前 max_entries 作子树根
      3. 每个根 build_subtree_for_entry
      4. 不在任何子树里的剩余候选 → 孤儿 top-max_orphans
      5. notes：多入口时加桥接提示
      6. token 估算 → 超阈值 fallback_to_flat=True

    Args:
        candidates: recall 候选 dict 列表（含 entity_id, summary_text, module?, score?）
        code_snippets: {entity_id: source}（P1 grounding，可能为空）
        graph: GraphProto-like
    Returns:
        CandidateTree
    """
    if not candidates:
        return CandidateTree(subtrees=[], orphans=[], fallback_to_flat=False, notes=[])

    # 1. 提取 qn + meta
    candidate_qns: list[str] = []
    candidate_meta: dict[str, dict] = {}
    for c in candidates:
        eid = c.get("entity_id")
        if not eid:
            continue
        qn = _strip_signature(eid)
        if qn in candidate_meta:
            continue                                     # 同 qn 重复（带不带参）跳后者
        candidate_qns.append(qn)
        summary = c.get("summary_text") or ""
        if len(summary) > max_summary_chars:
            summary = summary[:max_summary_chars] + "…"
        candidate_meta[qn] = {
            "summary": summary,
            "module": c.get("module"),
            "code_snippet": code_snippets.get(eid) or code_snippets.get(qn),
        }

    # 2. 独立入口（取前 max_entries 作子树根）
    independent = compute_independent_entries(candidate_qns, graph, max_depth=max_depth)
    subtree_roots = independent[:max_entries]

    # 3. 构造每棵子树
    subtrees = [
        build_subtree_for_entry(
            entry, candidate_meta, graph,
            max_depth=max_depth,
            max_nodes_per_subtree=max_nodes_per_subtree,
        )
        for entry in subtree_roots
    ]

    # 4. 孤儿：剩余候选（不在任何子树里）→ 按原 recall 顺序取前 max_orphans
    in_subtree_qns: set[str] = set()
    for st in subtrees:
        in_subtree_qns.update(_collect_all_tree_qns(st))
    orphan_qns = [q for q in candidate_qns if q not in in_subtree_qns]
    orphans = [
        TreeNode(
            entity_id=q,
            summary=candidate_meta[q]["summary"],
            module=candidate_meta[q]["module"],
            code_snippet=candidate_meta[q]["code_snippet"],
            children=[],
        )
        for q in orphan_qns[:max_orphans]
    ]

    # 5. notes：多入口提示
    notes: list[str] = []
    if len(subtrees) >= 2:
        notes.append(
            "多入口检测：识别到 %d 个独立业务路径；路径间可能通过 MQ / Spring 配置 / AOP 异步桥接，"
            "CodeGraph 静态分析无法连线，作答时用文字描述跨边界关系，不要编 calls 边。"
            % len(subtrees)
        )

    tree = CandidateTree(subtrees=subtrees, orphans=orphans, fallback_to_flat=False, notes=notes)

    # 6. token 估算 → 超阈值降扁平
    if _estimate_tokens_in_tree(tree) > token_safety_cap:
        # 保留 tree 内容供调试，但 fallback_to_flat=True 让调用方降级
        tree = CandidateTree(
            subtrees=tree.subtrees, orphans=tree.orphans,
            fallback_to_flat=True, notes=tree.notes,
        )
    return tree
```

- [ ] **Step 4: 跑绿** — `venv/bin/python -m pytest tests/test_auth/test_candidate_assembly.py -x`（期：15 passed）

- [ ] **Step 5: Commit**

```bash
git add src/service/qa_engine/candidate_assembly.py tests/test_auth/test_candidate_assembly.py
git commit -m "feat(qa): build_candidate_tree 编排 — 子树/孤儿/token cap/fallback（Task 4）"
```

---

### Task 5: RetrievedContext.candidate_tree + retriever 计算 + ctx → dict 透传

**Files:**
- Modify: `src/service/qa_engine/retriever.py`
- Modify: `src/service/qa_engine/synthesizer.py:_ctx_to_dict`
- Test: 复用 `tests/test_auth/test_retriever_*.py`（不一定需要新单测——retriever 改动是 wiring）

- [ ] **Step 1: 写失败测试** — 验证 retriever 调 build_candidate_tree 且写入 ctx.candidate_tree

```python
# tests/test_auth/test_candidate_assembly.py 追加（end-to-end wiring 测试）
def test_retriever_attaches_candidate_tree_to_ctx():
    """retriever.retrieve 完成后 ctx.candidate_tree 是 CandidateTree 实例（非 None）。"""
    # 用 mock store + mock graph 跑 retriever，断言 ctx.candidate_tree 存在
    from src.service.qa_engine.retriever import QARetriever, RetrievedContext
    from src.service.qa_engine.candidate_assembly import CandidateTree

    class _FakeStore:
        def search_method_hits_by_text(self, *, text, project_id, limit):
            return [
                {"entity_id": "OrderTimeOutCancelTask::cancelTimeOutOrder", "summary_text": "定时取消", "score": 0.78},
                {"entity_id": "OmsPortalOrderServiceImpl::generateOrder", "summary_text": "下单", "score": 0.76},
            ]
        def get_by_entity(self, eid):
            return None

    class _FakeG(_FakeGraph):
        def module_of(self, qn): return "mall-portal"
        def predecessors(self, qn, rel_type=None): return []

    retriever = QARetriever(
        interpretation_store=_FakeStore(),
        graph=_FakeG({}),
        recall_threshold=0.5,
    )
    import asyncio
    ctx = asyncio.run(retriever.retrieve(question="订单超时", project_id="p", top_k=2))
    assert isinstance(ctx.candidate_tree, CandidateTree)
    assert len(ctx.candidate_tree.subtrees) == 2  # 两个独立入口
```

- [ ] **Step 2: 跑红** — 期：`AttributeError: 'RetrievedContext' object has no attribute 'candidate_tree'`

- [ ] **Step 3: 改 RetrievedContext + retriever.retrieve**

读 `src/service/qa_engine/retriever.py:86` 找 RetrievedContext 类定义，加字段：

```python
# RetrievedContext dataclass 内追加
candidate_tree: Optional["CandidateTree"] = None    # 候选树（Task 5）
```

顶部 import：
```python
from src.service.qa_engine.candidate_assembly import build_candidate_tree, CandidateTree
```

`retrieve` 方法末尾（C2 callchain 部分之后、return ctx 之前）追加：

```python
        # 候选树（[[候选按调用顺序组装-设计]]）：识别独立入口 + BFS 子树 + 孤儿
        # 喂给 build_user_prompt 渲染按调用顺序分组的候选区
        try:
            ctx.candidate_tree = build_candidate_tree(
                ctx.entry_candidates,
                code_snippets=getattr(ctx, "candidate_code_snippets", {}) or {},
                graph=self.graph,
            )
        except Exception as e:
            # 任何异常一律不阻断 retrieve；prompt 端见 None 走旧扁平分支
            import logging
            logging.getLogger(__name__).warning("build_candidate_tree 失败: %s", e)
            ctx.candidate_tree = None
```

`src/service/qa_engine/synthesizer.py:_ctx_to_dict` 追加一行：

```python
        # 候选树（[[候选按调用顺序组装-设计]]）：build_user_prompt 据此选 tree / flat 分支
        "candidate_tree": getattr(ctx, "candidate_tree", None),
```

- [ ] **Step 4: 跑绿** — `venv/bin/python -m pytest tests/test_auth/test_candidate_assembly.py -x`（期：16 passed）

- [ ] **Step 5: Commit**

```bash
git add src/service/qa_engine/retriever.py src/service/qa_engine/synthesizer.py tests/test_auth/test_candidate_assembly.py
git commit -m "feat(qa): retriever 计算 candidate_tree 挂 ctx + _ctx_to_dict 透传（Task 5）"
```

---

### Task 6: prompts.build_user_prompt — tree 分支 + _render_tree_candidates

**Files:**
- Modify: `src/service/qa_engine/prompts.py`（候选区改造）
- Test: `tests/test_auth/test_prompts_candidate_tree.py`（新增）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_auth/test_prompts_candidate_tree.py
"""prompt 候选区树形渲染不变量测试。设计 [[候选按调用顺序组装-设计]] §4.3。

不验真实 LLM 输出，验 prompt 文本结构——多入口 → 树形分组标志 / 单入口 → 扁平 / 孤儿
附录 / 桥接 note 出现在 prompt 里。
"""
from src.service.qa_engine.candidate_assembly import TreeNode, CandidateTree
from src.service.qa_engine.prompts import build_user_prompt


def _tree_ctx(tree: CandidateTree, candidates: list[dict]) -> dict:
    """构造 build_user_prompt 用的最小 ctx dict。"""
    return {
        "entry_candidates": candidates,
        "candidate_tree": tree,
        "candidate_code_snippets": {},
        "callees_by_entry": {},
        "callers_by_entry": {},
        "call_edges_by_entry": {},
        "table_access_by_entry": {},
        "skill_id": "architecture",
    }


def test_multi_entry_renders_subtree_headers():
    """两棵子树 → prompt 含【子树 1】【子树 2】标题 + 缩进 ├─ └─ 树形。"""
    sub1 = TreeNode(
        entity_id="Cls1::m", summary="下单", module="mall-portal", code_snippet=None,
        children=[TreeNode(entity_id="Cls1::sub", summary="下游", module=None, code_snippet=None, children=[])],
    )
    sub2 = TreeNode(
        entity_id="Task::scan", summary="定时", module="mall-portal", code_snippet=None,
        children=[],
    )
    tree = CandidateTree(subtrees=[sub1, sub2], orphans=[], fallback_to_flat=False,
                          notes=["多入口检测：识别到 2 个独立业务路径"])
    candidates = [
        {"entity_id": "Cls1::m", "summary_text": "下单"},
        {"entity_id": "Task::scan", "summary_text": "定时"},
    ]
    prompt = build_user_prompt("订单怎么取消", _tree_ctx(tree, candidates))
    assert "【子树 1】" in prompt
    assert "【子树 2】" in prompt
    # 树形符号
    assert "└─" in prompt or "├─" in prompt
    # note 进 prompt
    assert "多入口" in prompt


def test_single_entry_uses_flat():
    """单棵子树 → 走扁平分支（保旧行为）。"""
    sub1 = TreeNode(entity_id="Cls::m", summary="一", module=None, code_snippet=None, children=[])
    tree = CandidateTree(subtrees=[sub1], orphans=[], fallback_to_flat=False, notes=[])
    candidates = [{"entity_id": "Cls::m", "summary_text": "一"}]
    prompt = build_user_prompt("问题", _tree_ctx(tree, candidates))
    # 扁平分支 → 不含子树标题，沿用原文案"按相关度倒序"
    assert "【子树 1】" not in prompt
    assert "按相关度倒序" in prompt


def test_orphan_appendix_rendered():
    """孤儿 → prompt 含【其他相关实体（未连入主路径）】段。"""
    sub1 = TreeNode(entity_id="A::m", summary="A", module=None, code_snippet=None, children=[])
    sub2 = TreeNode(entity_id="B::m", summary="B", module=None, code_snippet=None, children=[])
    orphan = TreeNode(entity_id="CancelOrderReceiver::handle", summary="MQ 监听器",
                      module="mall-portal", code_snippet=None, children=[])
    tree = CandidateTree(subtrees=[sub1, sub2], orphans=[orphan], fallback_to_flat=False, notes=[])
    candidates = [
        {"entity_id": "A::m"}, {"entity_id": "B::m"},
        {"entity_id": "CancelOrderReceiver::handle"},
    ]
    prompt = build_user_prompt("Q", _tree_ctx(tree, candidates))
    assert "其他相关实体" in prompt
    assert "CancelOrderReceiver::handle" in prompt


def test_fallback_to_flat_when_tree_none():
    """ctx.candidate_tree=None → 走扁平（向后兼容）。"""
    candidates = [{"entity_id": "A::m", "summary_text": "A"}]
    ctx = _tree_ctx(CandidateTree(subtrees=[], orphans=[], fallback_to_flat=False, notes=[]), candidates)
    ctx["candidate_tree"] = None                      # 显式
    prompt = build_user_prompt("Q", ctx)
    assert "按相关度倒序" in prompt
    assert "【子树" not in prompt


def test_fallback_to_flat_when_tree_signals():
    """tree.fallback_to_flat=True → 走扁平（token 超阈值兜底）。"""
    sub1 = TreeNode(entity_id="A::m", summary="A", module=None, code_snippet=None,
                    children=[TreeNode(entity_id="B::m", summary="B", module=None, code_snippet=None, children=[])])
    sub2 = TreeNode(entity_id="C::m", summary="C", module=None, code_snippet=None, children=[])
    tree = CandidateTree(subtrees=[sub1, sub2], orphans=[], fallback_to_flat=True, notes=[])
    candidates = [{"entity_id": "A::m"}, {"entity_id": "B::m"}, {"entity_id": "C::m"}]
    prompt = build_user_prompt("Q", _tree_ctx(tree, candidates))
    assert "按相关度倒序" in prompt                   # 扁平
    assert "【子树" not in prompt
```

- [ ] **Step 2: 跑红** — `venv/bin/python -m pytest tests/test_auth/test_prompts_candidate_tree.py -x`（期：5 failed，全部因没有"【子树"）

- [ ] **Step 3: 实现 prompts.py 改造**

读 `src/service/qa_engine/prompts.py:290-340` 找当前候选区代码。改成：

```python
# src/service/qa_engine/prompts.py 在 build_user_prompt 内候选区位置
# 原代码（约 290 行起）：
#     candidates = context.get("entry_candidates") or []
#     code_snippets = context.get("candidate_code_snippets") or {}
#     if candidates:
#         parts.append("")
#         parts.append("候选入口方法（按相关度倒序）:")
#         ... 扁平渲染 ...

# 改为：
    candidates = context.get("entry_candidates") or []
    code_snippets = context.get("candidate_code_snippets") or {}
    candidate_tree = context.get("candidate_tree")
    if candidates:
        parts.append("")
        # 候选树分支判断：候选树存在 + 多于一棵子树 + 不要求 fallback → 走树形；否则扁平
        # 设计 [[候选按调用顺序组装-设计]] §4.3
        if (
            candidate_tree is not None
            and len(candidate_tree.subtrees) >= 2
            and not candidate_tree.fallback_to_flat
        ):
            parts.extend(_render_tree_candidates(candidate_tree, code_snippets))
        else:
            parts.extend(_render_flat_candidates(candidates, code_snippets))
```

把原扁平代码（约 295-340 行的循环 + 渲染）抽到一个新 helper 函数 `_render_flat_candidates`：

```python
# src/service/qa_engine/prompts.py 文件末尾追加
def _render_flat_candidates(candidates: list[dict], code_snippets: dict) -> list[str]:
    """扁平候选区渲染（旧行为）：从原 build_user_prompt 内提取，保字面一致。"""
    parts: list[str] = []
    parts.append("候选入口方法（按相关度倒序）:")
    if code_snippets:
        parts.append(
            "  （注：以下候选凡附【真实源码片段】的，代码细节——SQL/表名/字段/存储技术/方法调用/状态码"
            "——一律以源码为准，2b 业务说明仅作业务提示、不可当代码事实；引用仍用 entity_id。）"
        )
    top_candidates = candidates[:TOP_CANDIDATES_FOR_PROMPT]
    for i, c in enumerate(top_candidates, 1):
        entity_id = c.get("entity_id", "?")
        level = c.get("level", "method")
        module = c.get("module")
        summary = c.get("summary_text") or "(无业务说明)"
        if len(summary) > 300:
            summary = summary[:300] + "…"
        mod_str = f"  (模块: {module})" if module else ""
        parts.append(f"  {i}. {entity_id}  [level={level}]{mod_str}")
        parts.append(f"     业务说明: {summary}")
        snippet = code_snippets.get(entity_id)
        if snippet:
            parts.append("     【真实源码片段】(代码细节以此为准):")
            parts.append("     ```")
            for line in snippet.splitlines():
                parts.append(f"     {line}")
            parts.append("     ```")
    if any(c.get("module") for c in top_candidates):
        parts.append("")
        parts.append("【模块判断指引】候选条目里 (模块: x) 字段标注该实体属于哪个模块（mall-portal=前台、mall-admin=后台）；不要凭名字猜，按模块字段判前台/后台。")
    return parts


def _render_tree_candidates(tree, code_snippets: dict) -> list[str]:
    """树形候选区渲染（[[候选按调用顺序组装-设计]] §4.3）。"""
    parts: list[str] = []
    parts.append("候选入口方法（按调用子树分组，每个子树按调用顺序）:")
    if code_snippets:
        parts.append(
            "  （注：以下候选凡附【真实源码片段】的，代码细节——SQL/表名/字段/存储技术/方法调用/状态码"
            "——一律以源码为准，2b 业务说明仅作业务提示、不可当代码事实；引用仍用 entity_id。）"
        )
    # 渲染每棵子树
    for i, root in enumerate(tree.subtrees, 1):
        parts.append("")
        mod_str = f"  (模块: {root.module})" if root.module else ""
        parts.append(f"【子树 {i}】入口: {root.entity_id}{mod_str}")
        if root.summary:
            parts.append(f"  业务说明: {root.summary}")
        _render_tree_children(parts, root.children, prefix="")
        # 子树根的 code_snippet 也渲染
        if root.code_snippet:
            parts.append("  【真实源码片段】(代码细节以此为准):")
            parts.append("  ```")
            for line in root.code_snippet.splitlines():
                parts.append(f"  {line}")
            parts.append("  ```")
    # 孤儿附录
    if tree.orphans:
        parts.append("")
        parts.append("【其他相关实体（未连入主路径）】")
        for o in tree.orphans:
            mod_str = f"  (模块: {o.module})" if o.module else ""
            parts.append(f"  - {o.entity_id}{mod_str}")
            if o.summary:
                parts.append(f"    业务说明: {o.summary}")
    # notes
    if tree.notes:
        parts.append("")
        for note in tree.notes:
            parts.append(f"【说明】{note}")
    return parts


def _render_tree_children(parts: list[str], children: list, prefix: str) -> None:
    """递归渲染子树缩进。用 ├─ └─ 表示层级。"""
    n = len(children)
    for i, child in enumerate(children):
        is_last = (i == n - 1)
        branch = "└─" if is_last else "├─"
        mod_str = f"  (模块: {child.module})" if child.module else ""
        parts.append(f"  {prefix}{branch} {child.entity_id}{mod_str}")
        if child.summary:
            indent = "   " if is_last else "│  "
            parts.append(f"  {prefix}{indent}业务说明: {child.summary}")
        # 递归子节点
        new_prefix = prefix + ("   " if is_last else "│  ")
        _render_tree_children(parts, child.children, new_prefix)
```

- [ ] **Step 4: 跑绿** — `venv/bin/python -m pytest tests/test_auth/test_prompts_candidate_tree.py -x`（期：5 passed）

- [ ] **Step 5: Commit**

```bash
git add src/service/qa_engine/prompts.py tests/test_auth/test_prompts_candidate_tree.py
git commit -m "feat(qa): prompts.build_user_prompt 候选区树形分支 + _render_tree_candidates（Task 6）"
```

---

### Task 7: 全量回归

- [ ] **Step 1: 跑全量后端测试**

```bash
cd /Users/java/knowledge-engineering-auth
venv/bin/python -m pytest tests/test_auth/ -q
```

期：**全绿**。如果有失败，逐个修：
- 若现有 prompt 测试因字面变化（"按相关度倒序" 句子位置改变）失败 → 是字面对齐问题，确认逻辑没变后微调测试断言
- 若 retriever 测试因 ctx.candidate_tree 新字段失败 → 给测试夹具默认 None 兜底

- [ ] **Step 2: tsc / vitest 兼容性检查（前端）** — 本次不动前端，跳过 / 仅快速 `cd /Users/java/knowledge-engineering-web && npx tsc --noEmit` 确保无副作用

- [ ] **Step 3: Commit（若有回归修复）**

```bash
git commit -am "test: 候选树改造回归收尾（Task 7）"
```

---

### Task 8: 部署 + 10 题手测核验（需授权，不自动）

- [ ] **Step 1: push origin/release-0513**

```bash
cd /Users/java/knowledge-engineering-auth
git push origin release-0513
```

- [ ] **Step 2: bundle + scp + ff-merge + restart + health**（**需用户授权**）

```bash
# 起点 commit 改成实际上一次部署的 HEAD（部署前 ssh 上去 git log --oneline -1 确认）
LAST_DEPLOY=$(ssh -p 26666 root@103.47.81.50 'cd /opt/knowledge-engineering && git rev-parse --short HEAD')
git bundle create /tmp/ke-deploy-candidate-tree.bundle "${LAST_DEPLOY}..origin/release-0513"
scp -P 26666 /tmp/ke-deploy-candidate-tree.bundle root@103.47.81.50:/tmp/
ssh -p 26666 root@103.47.81.50 '
  set -e
  cd /opt/knowledge-engineering
  git fetch /tmp/ke-deploy-candidate-tree.bundle refs/remotes/origin/release-0513:incoming
  git merge --ff-only incoming
  git branch -d incoming
  systemctl restart ke-api
  sleep 3
  systemctl is-active ke-api
  curl -s --max-time 5 http://127.0.0.1:8000/health
'
```

- [ ] **Step 3: 10 题手测核验** — 浏览器问以下题，对调用图边做 CodeGraph 核验

10 题清单（覆盖多入口 / 跨边界 / 多模块）：

1. 订单超时自动取消是怎么实现的？（本次案例 - 期望路径 A / 路径 B 分开、伪边 0）
2. 用户下单的完整流程是怎样的？（多层 Controller → Service → Dao）
3. 后台订单管理 vs 前台下单（多入口 / 多模块）
4. 后台管理员 vs 前台会员登录（Q14，多入口 / 多模块）
5. 支付成功后的业务处理（多下游 callees）
6. SKU 库存扣减 / 释放（双触发：下单扣 + 取消释）
7. 商品上架与下架流程
8. 收货地址 CRUD + 越权校验
9. 退货提交审核流程
10. 积分增减场景（多触发点：消费 / 取消 / 退款）

每题完成后用 `verify_call_graph.py` 风格的脚本核验：列出图里所有边，逐条对照 CodeGraph 真实 calls 边集合，统计**跨路径伪边数**。

- [ ] **Step 4: 验收门** — 10 题里跨路径伪边总数 ≤ 5（< 5% target）则 ✅；否则反推 prompt 还需哪些改进，回 Task 6 微调。

---

### Task 9: （可选）30 题评测重跑

- [ ] **Step 1**: 用现有评测脚本重跑 `mall-swarm-QA评测报告-第二轮` 的 30 题
- [ ] **Step 2**: 对照 Q5 / Q14 等多入口题的质量等级（A/B/C）变化
- [ ] **Step 3**: 写一份评测报告挂在 Obsidian 作为本设计的"实施后量化结果"

---

### Task 10: Obsidian "已实施" 标记

- [ ] **Step 1**: 改 [[候选按调用顺序组装-设计]] header `status: 已上线 ldclouda30562（YYYY-MM-DD）`
- [ ] **Step 2**: 改 [[_overview]] 对应条目 status 同步
- [ ] **Step 3**: 加 commit hash + 评测前/后对比（若 Task 9 跑了）

---

## Self-Review

- **Spec 覆盖**：
  - §三 目标 1（树形候选区）→ Task 3 + 4 + 6 实现
  - §三 目标 2（跨子树桥接 note）→ Task 4 实现（multi-entry → notes 追加）
  - §三 目标 3（LLM 不编跨路径伪边）→ Task 8 手测验证
  - §三 目标 4（prompt 结构可单测）→ Task 6 5 个单测
  - §三 目标 5（30 题评测可选）→ Task 9
- **Spec §五 错误处理**：5 种降级路径都覆盖
  - graph 不可用 → Task 2/3 try/except + Task 5 异常兜底
  - 候选 < 2 → Task 6 prompt 分支逻辑（len(subtrees) >= 2 才走树形）
  - token 超阈值 → Task 4 estimate + fallback_to_flat
  - 全连通（独立入口仅 1）→ Task 6 走扁平
  - 顶层异常 → Task 5 try/except
- **Placeholder scan**: 无 TBD / TODO / "适当处理"；所有代码都展示
- **Type consistency**:
  - `TreeNode`（Task 1）字段 entity_id / summary / module / code_snippet / children 在 Task 3/4/6 一致引用
  - `CandidateTree`（Task 1）字段 subtrees / orphans / fallback_to_flat / notes 一致
  - `compute_independent_entries`（Task 2）签名 (candidate_qns, graph, max_depth=3) → list[str] 在 Task 4 调用时签名匹配
  - `build_subtree_for_entry`（Task 3）签名 (entry_qn, candidate_meta, graph, max_depth=3, max_nodes_per_subtree=6) 在 Task 4 调用时匹配
  - `build_candidate_tree`（Task 4）签名 (candidates, code_snippets, graph, ...) 在 Task 5 retriever 调用时匹配
- **算法复杂度**: K=10 候选两两比较 100 对，每对内 BFS depth=3 ≤ 100 节点访问 → 共 ~10000 节点访问 / 请求；纯内存，预计 < 10ms 可接受
- **回归保护**: 单入口 / candidate_tree=None / fallback_to_flat=True 都走扁平分支，保字面一致行为
- **依赖**: GraphProto 已有 successors / predecessors / module_of，无需扩接口

---
