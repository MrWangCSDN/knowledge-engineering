"""检索阶段：业务概念 → 候选入口方法 + 调用链上下文。

跨仓依赖说明：
  实际的 business_store / graph 实例来自 knowledge-engineering 主仓
  （src/knowledge/weaviate_business_store.py 和 src/knowledge/__init__.py）。
  本仓只用 Protocol 定义"我期望它有什么方法"，运行时 api.py 启动注入实例。
  这样 auth 仓不需要 import 主仓代码，编译/单测可独立。

设计文档：[[首页设计]] §6.1
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


# ─── 结构类型（Protocol）─ 不导入主仓，只定义"接口"────────────────────────

class BusinessStoreProto(Protocol):
    """跟 src/knowledge/weaviate_business_store.py:BusinessInterpretationStore 兼容。"""

    def search_method_hits_by_text(
        self, *, text: str, project_id: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        ...


class GraphProto(Protocol):
    """跟 src/knowledge/graph.py:KnowledgeGraph 兼容。"""

    def successors(self, entity_id: str, rel_type: str | None = None) -> list[str]: ...
    def predecessors(self, entity_id: str, rel_type: str | None = None) -> list[str]: ...


# ─── 检索结果数据结构 ──────────────────────────────────────────────────────

@dataclass
class RetrievedContext:
    """喂给 synthesizer 作为 LLM context 的完整资料包。"""

    question: str
    project_id: str
    entry_candidates: list[dict[str, Any]] = field(default_factory=list)
    """BusinessInterpretation 命中（含 entity_id / summary_text / level）。"""

    callees_by_entry: dict[str, list[str]] = field(default_factory=dict)
    """{ entity_id: [下游 method id, ...] }。仅 top-3 候选取调用链以控制成本。"""

    callers_by_entry: dict[str, list[str]] = field(default_factory=dict)
    """{ entity_id: [上游 caller id, ...] }。"""

    table_access_by_entry: dict[str, list[dict]] = field(default_factory=dict)
    """{ entity_id: [{table_id, operation}, ...] }。Mode B 需要的数据访问信息。"""


# ─── 检索器 ─────────────────────────────────────────────────────────────────

class QARetriever:
    """从 Weaviate（语义）+ 图谱（拓扑）取候选实体和调用链。"""

    # 控制成本：只对 top-N 候选取调用链
    TOP_N_FOR_CHAIN_EXPANSION = 3
    # 控制 context 长度：每个方向只取前 5 跳
    MAX_CALLEES = 5
    MAX_CALLERS = 5

    def __init__(self, *, business_store: BusinessStoreProto, graph: GraphProto):
        self.business_store = business_store
        self.graph = graph

    async def retrieve(
        self,
        *,
        question: str,
        project_id: str,
        top_k: int = 5,
    ) -> RetrievedContext:
        """主入口。

        Steps:
          1. 用问题去 BusinessInterpretation 向量库做语义检索（带 project_id 过滤）
          2. 对 top-N 候选，从图谱取上下游 1 跳调用关系
          3. 提取数据表访问（best-effort，失败不抛错）
        """
        ctx = RetrievedContext(question=question, project_id=project_id)

        # 1. 语义检索候选实体
        candidates = self.business_store.search_method_hits_by_text(
            text=question, project_id=project_id, limit=top_k
        )
        ctx.entry_candidates = candidates

        # 2. 对 top-N 候选取调用链
        for c in candidates[: self.TOP_N_FOR_CHAIN_EXPANSION]:
            entity_id = c.get("entity_id")
            if not entity_id:
                continue

            # 调用链向下（callees）
            try:
                ctx.callees_by_entry[entity_id] = list(
                    self.graph.successors(entity_id)
                )[: self.MAX_CALLEES]
            except Exception:
                # 图节点不存在 / 后端连不上：静默跳过
                ctx.callees_by_entry[entity_id] = []

            # 调用链向上（callers）
            try:
                ctx.callers_by_entry[entity_id] = list(
                    self.graph.predecessors(entity_id)
                )[: self.MAX_CALLERS]
            except Exception:
                ctx.callers_by_entry[entity_id] = []

            # 数据库访问（best-effort）
            ctx.table_access_by_entry[entity_id] = self._extract_table_access(entity_id)

        return ctx

    def _extract_table_access(self, entity_id: str) -> list[dict]:
        """从图谱里提取这个方法访问的数据表。

        约定：图上有 'accesses_table' 边类型（由 pipeline 写入）。
        没有该信息时返回空列表。
        """
        try:
            tables: list[dict] = []
            for table_id in self.graph.successors(entity_id, rel_type="accesses_table"):
                tables.append({"table_id": table_id, "operation": "unknown"})
            return tables
        except Exception:
            return []
