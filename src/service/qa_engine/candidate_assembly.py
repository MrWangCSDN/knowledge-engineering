"""候选按调用顺序组装：把召回扁平 candidates 重组为按业务子树分组的 prompt 表示。

设计 [[候选按调用顺序组装-设计]] §4.2。核心：
  - compute_independent_entries：从候选里挑"不被其他候选 BFS 下游包含"的真正独立入口
  - build_subtree_for_entry：从入口 BFS depth=3，优先候选成员入子树
  - build_candidate_tree：编排——多子树 + 孤儿 + token 估算 + 降扁平兜底

整体定位：纯函数模块——不持状态、不读外部存储、所有依赖通过参数注入。
"""
# PEP 563：注解延迟求值，让类型注解里可以提前引用未定义的类名（如 TreeNode 内部 list["TreeNode"]）
from __future__ import annotations

# deque：BFS 队列（同 ke_impact / render_call_graph 的遍历范式）
from collections import deque

# dataclass 装饰器 + field：给字段设默认工厂（如 list[TreeNode] 默认 []）
from dataclasses import dataclass, field

# Optional[X] = X | None，签名兼容旧 Python 写法
from typing import Optional


# ── 数据载体（dataclass）─────────────────────────────────────────────────────
# 用 dataclass 而非纯 dict：① 类型可见 ② 字段名稳定（dict 拼写错误难发现）
# 非 frozen：BFS 构造子树时需要 append children；frozen 会让 .children.append 安全但
# 妨碍可读性（构造函数里全列出来更繁琐）。生产代码慎写但本模块构造完只读，可控。


@dataclass
class TreeNode:
    """候选树的一个节点。

    设计 §4.2：节点字段对齐 prompt 渲染所需。其中：
      - entity_id：候选 qualified_name（已剥 # 签名），同 CodeGraph 的 qn 命名
      - summary：业务说明（已按 max_summary_chars 截断）
      - module：模块名（如 mall-portal / mall-admin），None 表示未知
      - code_snippet：source-first grounding P1 命中的真实源码片段；None 表示无
      - children：子节点列表（BFS 下游候选成员；默认空 list）
    """
    entity_id: str
    summary: str
    module: Optional[str]
    code_snippet: Optional[str]
    # field(default_factory=list)：避免可变默认陷阱（"@dataclass 时 children=[] 写法会让所有实例共享同一 list"）
    children: list["TreeNode"] = field(default_factory=list)


@dataclass
class CandidateTree:
    """build_candidate_tree 的产出。

    四字段含义：
      - subtrees：独立入口对应的子树（最多 max_entries 棵，按 recall 顺序）
      - orphans：孤儿候选（无 children；按 recall 顺序取前 max_orphans）
      - fallback_to_flat：token 估算超阈值 → True；调用方（prompt builder）应降级为扁平
      - notes：元信息字符串列表（如"多入口检测：…可能 MQ 异步桥接，CodeGraph 抓不到"）
    """
    subtrees: list[TreeNode]
    orphans: list[TreeNode]
    fallback_to_flat: bool
    notes: list[str]


# ── 算法 ────────────────────────────────────────────────────────────────────


def _strip_signature(durable_key: str) -> str:
    """剥 # 签名。CodeGraph durable_key 形如 'Cls::m#(p)'，比较 qn 时需归一。

    Args:
        durable_key: 完整持久 key，可能形如 'Cls::m#(Long)' 或裸 'Cls::m'
    Returns:
        去掉 '#' 及之后的内容；不含 '#' 时原样返回
    """
    # str.split(sep, maxsplit=1)：最多切 1 刀，[0] 取 '#' 前的部分
    # 'Cls::m#()' → ['Cls::m', '()']  → 'Cls::m'
    # 'Cls::m'    → ['Cls::m']         → 'Cls::m'（取首即原值）
    return durable_key.split("#", 1)[0]


def _bfs_reachable(start: str, graph, max_depth: int) -> set[str]:
    """BFS 收集 start 下游可达节点的 qn 集合（不含 start 自身）。

    用于 compute_independent_entries 判定"候选 c 在候选 d 的下游可达集合里"。

    Args:
        start: 起始 qualified_name（已剥签名）
        graph: 实现 successors(qn) → list[durable_key] 的对象（GraphProto-like）
        max_depth: 最大 BFS 深度（深度 0 = 起点，1 = 直接下游；max_depth=3 时收集到 3 跳）
    Returns:
        所有可达 qn 的集合（已剥签名）；图后端异常 → 部分结果（best-effort）
    """
    reachable: set[str] = set()
    # deque：双端队列，O(1) popleft 比 list.pop(0) 快
    # 元组 (node, depth)：跟踪当前节点 + 距 start 的深度
    queue: deque[tuple[str, int]] = deque([(start, 0)])
    while queue:
        node, depth = queue.popleft()
        # 深度限制：已达 max_depth 的节点不再扩展（其下游不进 reachable）
        if depth >= max_depth:
            continue
        # try/except：图后端可能挂（sqlite 异常 / NullGraph 返 []）；
        # 单个节点查失败不阻断整体 BFS（fail-soft，与 ke_impact 同风格）
        try:
            # successors 返带签名的 durable_key 列表（可能空）
            # `or []`：防 successors 返 None 时 for 循环抛 TypeError
            succs = graph.successors(node) or []
        except Exception:
            succs = []
        for s in succs:
            qn = _strip_signature(s)             # 归一：剥签名后做 qn 比较
            # 排除自环 + 去重：start 自身不进 reachable；已访问过的不重复入队
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
    """从候选 qn 列表识别"独立入口"——不在任何其他候选下游 BFS 可达集里的。

    设计 [[候选按调用顺序组装-设计]] §4.2 算法 ①：双向 reach 集合互查。
      c 是独立入口 iff ∀ d ∈ candidates, d ≠ c → c ∉ reach[d]

    保序：按 candidate_qns 原顺序输出（让首入口为召回分最高的）。

    Args:
        candidate_qns: 候选 qn 列表（调用方应预先剥签名；本函数也容忍带签名输入）
        graph: GraphProto-like，提供 successors(qn, rel_type=None) → list[str]
        max_depth: BFS 深度上限（默认 3 跳；过深可能导致大图爆炸）
    Returns:
        独立入口 qn 列表（原序的子集）；空输入 → 空列表
    """
    # 防御性归一：调用方可能传带签名的 durable_key；做一次 strip 让算法内部统一
    normalized = [_strip_signature(c) for c in candidate_qns]

    # 1. 每个候选预算 BFS 下游可达集合
    # 字典推导式：reach[c] = c 的 BFS 下游 qn 集合
    # 复杂度：K 候选 × 每个 BFS O(depth^branching) 节点；K=10, depth=3 时约几千节点访问
    reach: dict[str, set[str]] = {
        c: _bfs_reachable(c, graph, max_depth) for c in normalized
    }

    # 2. 独立判定：c 不在任何其他 d 的 reach 集合里即为独立入口
    independent: list[str] = []
    for c in normalized:
        is_descendant = False
        for d in normalized:
            # 跳过自比较（c 必然不在自己的 reach 集里——_bfs_reachable 已排除）
            if d == c:
                continue
            # 找到任一 d 把 c 当下游 → c 不是真入口
            if c in reach[d]:
                is_descendant = True
                break  # 短路：一个命中就够，无需继续
        if not is_descendant:
            independent.append(c)
    return independent


def build_subtree_for_entry(
    entry_qn: str,
    candidate_meta: dict[str, dict],
    graph,
    *,
    max_depth: int = 3,
    max_nodes_per_subtree: int = 6,
) -> TreeNode:
    """从 entry 出发 BFS 构造一棵候选子树（候选优先入子树，非候选下游跳过）。

    设计 [[候选按调用顺序组装-设计]] §4.2 算法 ②。要点：
      - 严格只挂 candidate_meta 里的成员节点，避免子树爆出候选边界引入 LLM 未见过的 entity
      - 候选成员优先：同层 successors 中候选排前，确保命中名额给真正召回过的 entity
      - 节点上限：达到 max_nodes_per_subtree 即停止扩展（含 root，防单子树过大）
      - fail-soft：graph 后端异常一律视为该节点无下游，仍返根

    Args:
        entry_qn: 入口 qualified_name（已剥签名；调用方负责归一）
        candidate_meta: {qn: {summary, module?, code_snippet?}}；只这些会被挂进子树
        graph: GraphProto-like，提供 successors(qn, rel_type=None) → list[durable_key]
        max_depth: BFS 深度（默认 3 跳：root + 3 层下游）
        max_nodes_per_subtree: 整棵子树节点上限（含根；默认 6）
    Returns:
        子树根 TreeNode（一定含 root；children 可能空）
    """
    # 候选成员 qn 集合，用于"优先入子树"判定和"非候选过滤"
    # set(...)：O(1) 成员判断；dict.keys() 在 Python 3.7+ 保序但 set 更适合本场景
    candidate_qns_set: set[str] = set(candidate_meta.keys())

    def _make_node(qn: str) -> TreeNode:
        """根据 qn 构造 TreeNode；从 candidate_meta 取元信息，缺则默认。

        闭包：直接读外层 candidate_meta，避免每次显式传参。
        """
        # dict.get(key, default)：键不存在时返回 default 而非抛 KeyError
        meta = candidate_meta.get(qn, {})
        return TreeNode(
            entity_id=qn,
            summary=meta.get("summary") or "",       # None / 空串都归一为 ""
            module=meta.get("module"),                # None 兜底
            code_snippet=meta.get("code_snippet"),    # None 兜底
            children=[],                              # 显式空 list（dataclass field default 也是空）
        )

    # 根节点构造（不依赖 graph，必成功）
    root = _make_node(entry_qn)

    # BFS 状态：访问集 + 节点查找表 + 队列 + 已添加节点计数
    # visited：去环、去重（同一 qn 不重复挂）
    visited: set[str] = {entry_qn}
    # node_lookup：qn → TreeNode，方便给 parent.children 追加 child
    node_lookup: dict[str, TreeNode] = {entry_qn: root}
    # 队列：(qn, depth_from_root)；root 在深度 0
    queue: deque[tuple[str, int]] = deque([(entry_qn, 0)])
    # nodes_added：已挂进子树的节点总数（含 root）；达上限即停
    nodes_added = 1

    while queue and nodes_added < max_nodes_per_subtree:
        current_qn, depth = queue.popleft()
        # 深度限制：已达 max_depth 的节点不再扩展（其下游不进树）
        if depth >= max_depth:
            continue
        # 取下游 successors；图后端异常 → 静默视为无下游
        try:
            succs = graph.successors(current_qn) or []
        except Exception:
            succs = []

        # 候选优先：把 successors 按"是否在候选里"排序——候选成员排前
        # 列表推导式 + sorted：稳定排序（key 相同时保原顺序）
        succ_qns = [_strip_signature(s) for s in succs]
        # 排序键：候选成员 = 0（排前），非候选 = 1（排后）
        # lambda：匿名函数，一行表达
        prioritized = sorted(
            succ_qns,
            key=lambda q: (0 if q in candidate_qns_set else 1),
        )

        for succ_qn in prioritized:
            # 上限触发 → 停止挂新节点（剩下的 successors 都跳过）
            if nodes_added >= max_nodes_per_subtree:
                break
            # 去重：同 qn 已挂过 → 跳
            if succ_qn in visited:
                continue
            # 严格候选过滤：非候选成员一律不挂进子树
            # （即便它在 successors 里，避免子树突破召回边界）
            if succ_qn not in candidate_qns_set:
                continue
            # 真正挂载：构造 child 节点 + 写 visited + 加入父节点 children 列表
            visited.add(succ_qn)
            child = _make_node(succ_qn)
            # 父节点查 node_lookup；append 是 list 方法、直接 mutation
            node_lookup[current_qn].children.append(child)
            node_lookup[succ_qn] = child
            nodes_added += 1
            # 入队继续 BFS（深度 + 1）
            queue.append((succ_qn, depth + 1))

    return root
