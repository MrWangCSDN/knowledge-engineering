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
