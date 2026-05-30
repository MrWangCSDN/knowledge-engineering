# src/integrations/codegraph/graph_adapter.py
"""把 CodeGraphDB 包成 KE 的 GraphProto（successors/predecessors）。

GraphProto 的 entity_id 用 KE 的持久身份(durable_key)；
本 adapter 负责 durable_key ↔ CodeGraph 节点 的解析，返回也用 durable_key。
"""
from __future__ import annotations  # PEP 563：允许类型注解里提前引用未定义的类名

from typing import Optional  # Optional[X] 表示值可以是 X 或 None
from src.integrations.codegraph.db import CodeGraphDB, CgNode  # 只读 DB 访问层与节点数据类
from src.integrations.codegraph.durable_key import durable_key  # 持久身份派生函数

# 字典：GraphProto 的 rel_type（KE 语义）→ CodeGraph 边的 kind 字段
# None 表示调用方没传 rel_type，默认也走 'calls' 边
_REL_TO_KIND = {None: "calls", "calls": "calls", "CALLS": "calls"}


class CodeGraphGraphAdapter:
    """实现 src.service.qa_engine.retriever.GraphProto，后端是只读 SQLite。

    接口约定：
    - successors(entity_id, rel_type=None) -> list[str]   出边（callees）的 durable_key 列表
    - predecessors(entity_id, rel_type=None) -> list[str] 入边（callers）的 durable_key 列表
    """

    def __init__(self, db: CodeGraphDB) -> None:
        """构造器：注入只读 DB 访问层（依赖注入模式，方便单测传 mock）。"""
        self._db = db  # 保存对 DB 的引用，后续查询都走这个对象

    def _resolve(self, entity_id: str) -> list[CgNode]:
        """把 durable_key 解析回 CodeGraph 节点列表。

        策略：
        1. '#' 前缀是 qualified_name → 用 DB 按 qualified_name 查找全部候选
        2. 候选唯一 → 直接返回（最常见情况）
        3. 候选多个（重载）→ 用完整 durable_key 精筛；精筛为空则退回全部（宁多不漏）

        Args:
            entity_id: 持久身份 key，格式为 'Cls::method#(params)' 或 'Cls::method'
        Returns:
            匹配的 CgNode 列表
        """
        # split('#', 1)：最多切 1 刀，[0] 取 '#' 前的 qualified_name 部分
        qn = entity_id.split("#", 1)[0]
        # 按 qualified_name 精确查找所有候选节点（可能包含重载）
        cands = self._db.find_nodes_by_qualified_name(qn)
        if len(cands) <= 1:             # 唯一或无结果 → 直接返回，无需精筛
            return cands
        # 多个候选（重载场景）：用完整 durable_key 再过滤一次
        # 列表推导式：保留 durable_key(n) 与传入 entity_id 完全匹配的节点
        exact = [n for n in cands if durable_key(n) == entity_id]
        # or 短路：精筛有结果则用精筛结果；否则退回全部候选（防漏）
        return exact or cands

    def _walk(self, entity_id: str, rel_type: Optional[str], direction: str) -> list[str]:
        """successors/predecessors 的公共实现，通过 direction 参数区分方向。

        Args:
            entity_id: 起点/终点节点的持久身份 key
            rel_type:  KE 关系类型，None 默认为 'calls'
            direction: 'succ'（出边）或 'pred'（入边）
        Returns:
            邻居节点的 durable_key 去重列表
        """
        # dict.get(key, default)：取 _REL_TO_KIND[rel_type]，未知类型 fallback 到 'calls'
        kind = _REL_TO_KIND.get(rel_type, "calls")
        out: list[str] = []    # 结果列表，保持插入顺序
        seen: set[str] = set() # 集合，O(1) 判重；不同 source 节点可能重复指向同一 target
        for node in self._resolve(entity_id):  # 遍历所有匹配节点（通常只有 1 个）
            # 根据方向选 successors（出边）或 predecessors（入边）
            neighbors = (
                self._db.successors(node.id, kind)
                if direction == "succ"
                else self._db.predecessors(node.id, kind)
            )
            for nb in neighbors:            # nb: neighbor，邻居节点 CgNode
                key = durable_key(nb)       # 把邻居节点转换为持久 key
                if key not in seen:         # 去重检查
                    seen.add(key)           # 标记已见
                    out.append(key)         # 加入结果
        return out

    def successors(self, entity_id: str, rel_type: Optional[str] = None) -> list[str]:
        """返回出边(callees)的 durable_key 列表（即 entity_id 调用了哪些节点）。

        Args:
            entity_id: 调用方节点的持久 key
            rel_type:  关系类型，默认 None（等效 'calls'）
        Returns:
            被调用节点的 durable_key 列表
        """
        return self._walk(entity_id, rel_type, "succ")  # 'succ' 方向 = 出边

    def predecessors(self, entity_id: str, rel_type: Optional[str] = None) -> list[str]:
        """返回入边(callers)的 durable_key 列表（即谁调用了 entity_id）。

        Args:
            entity_id: 被调用节点的持久 key
            rel_type:  关系类型，默认 None（等效 'calls'）
        Returns:
            调用方节点的 durable_key 列表
        """
        return self._walk(entity_id, rel_type, "pred")  # 'pred' 方向 = 入边
