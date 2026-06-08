"""候选按调用顺序组装：把召回扁平 candidates 重组为按业务子树分组的 prompt 表示。

设计 [[候选按调用顺序组装-设计]] §4.2。核心：
  - compute_independent_entries：从候选里挑"不被其他候选 BFS 下游包含"的真正独立入口
  - build_subtree_for_entry：从入口 BFS depth=3，优先候选成员入子树
  - build_candidate_tree：编排——多子树 + 孤儿 + token 估算 + 降扁平兜底

整体定位：纯函数模块——不持状态、不读外部存储、所有依赖通过参数注入。
"""
# PEP 563：注解延迟求值，让类型注解里可以提前引用未定义的类名（如 TreeNode 内部 list["TreeNode"]）
from __future__ import annotations

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
