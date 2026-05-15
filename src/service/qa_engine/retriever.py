"""检索阶段：业务概念 → 候选入口方法 + 调用链上下文。

跨仓依赖说明：
  实际的 business_store / graph 实例来自 knowledge-engineering 主仓
  （src/knowledge/weaviate_business_store.py 和 src/knowledge/__init__.py）。
  本仓只用 Protocol 定义"我期望它有什么方法"，运行时 api.py 启动注入实例。
  这样 auth 仓不需要 import 主仓代码，编译/单测可独立。

设计文档：[[首页设计]] §6.1
"""
from __future__ import annotations

# re：Python 标准库正则；这里用来从中文 summary_text 抽"<table> 表"模式
import re
from dataclasses import dataclass, field
from typing import Any, Protocol


# ─── 结构类型（Protocol）─ 不导入主仓，只定义"接口"────────────────────────

class BusinessStoreProto(Protocol):
    """跟 src/knowledge/weaviate_business_store.py:BusinessInterpretationStore 兼容。

    v1.2 起新增 get_by_entity，给 ke_business_interp tool 用。
    """

    def search_method_hits_by_text(
        self, *, text: str, project_id: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        ...

    def get_by_entity(
        self, entity_id: str, level: str | None = None
    ) -> dict[str, Any] | None:
        """按 entity_id 精确查一条业务解读；找不到返回 None。"""
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

    skill_id: str = "architecture"
    """v1.1 router 决策出来的 skill 名；synthesizer 据此往 user prompt 加视角偏置提示。"""


# ─── 检索器 ─────────────────────────────────────────────────────────────────

class QARetriever:
    """从 Weaviate（语义）+ 图谱（拓扑）取候选实体和调用链。

    skill 分支（v1.1）：
      - architecture（默认）：1 跳 callees + callers，性价比最高
      - dependency：2 跳 BFS 拓展，给 LLM 更完整的依赖图
      - data-flow / business 等：v1.1 后续 RED 测试再上
    """

    # 控制成本：只对 top-N 候选取调用链
    TOP_N_FOR_CHAIN_EXPANSION = 3
    # 控制 context 长度：每个方向只取前 5 跳
    MAX_CALLEES = 5
    MAX_CALLERS = 5
    # dependency skill 拓展深度
    DEPENDENCY_BFS_DEPTH = 2

    def __init__(self, *, business_store: BusinessStoreProto, graph: GraphProto):
        self.business_store = business_store
        self.graph = graph

    async def retrieve(
        self,
        *,
        question: str,
        project_id: str,
        top_k: int = 5,
        skill_id: str = "architecture",
    ) -> RetrievedContext:
        """主入口。

        Args:
            skill_id: 决定拓展策略；默认 architecture（1 跳）。
                'dependency' → callers/callees 2 跳 BFS
                其它 → 当前默认行为

        Steps:
          1. 用问题去 BusinessInterpretation 向量库做语义检索（带 project_id 过滤）
          2. 对 top-N 候选，按 skill_id 决定深度取上下游调用关系
          3. 提取数据表访问（best-effort，失败不抛错）
        """
        # v1.2: chit-chat 不需要 KG context，直接返回空 ctx（节省 latency + token）
        # 设计：[[chit-chat-闲聊路径-设计]] §4.2
        if skill_id == "chit-chat":
            return RetrievedContext(
                question=question,
                project_id=project_id,
                skill_id="chit-chat",
            )

        # 把 skill_id 一并存入 ctx，下游 synthesizer 据此调整 user prompt
        ctx = RetrievedContext(question=question, project_id=project_id, skill_id=skill_id)

        # 1. 语义检索候选实体
        candidates = self.business_store.search_method_hits_by_text(
            text=question, project_id=project_id, limit=top_k
        )

        # 1.5. business skill：重排，把 class/module 层级提到 method/api 前面
        # 直觉：业务规则类问题，class/module 级解读更贴题；method 级容易陷入技术细节
        if skill_id == "business":
            candidates = self._rerank_for_business(candidates)

        ctx.entry_candidates = candidates

        # 2. skill 切策略：dependency 用 2 跳 BFS，其它用 1 跳
        # 这种 if/else 分支在 skill 增加到 4-5 个时还可控；超过就 REFACTOR 抽 Strategy
        if skill_id == "dependency":
            # 调用链拓展深度从默认 1 跳升到 2 跳
            callee_depth = self.DEPENDENCY_BFS_DEPTH
            caller_depth = self.DEPENDENCY_BFS_DEPTH
        else:
            callee_depth = 1
            caller_depth = 1

        # 3. 对 top-N 候选取调用链
        for c in candidates[: self.TOP_N_FOR_CHAIN_EXPANSION]:
            entity_id = c.get("entity_id")
            if not entity_id:
                continue

            # 调用链向下（callees）
            ctx.callees_by_entry[entity_id] = self._bfs_chain(
                entity_id, direction="down", max_depth=callee_depth, max_nodes=self.MAX_CALLEES
            )
            # 调用链向上（callers）
            ctx.callers_by_entry[entity_id] = self._bfs_chain(
                entity_id, direction="up", max_depth=caller_depth, max_nodes=self.MAX_CALLERS
            )

            # 数据库访问（best-effort）
            base_tables = self._extract_table_access(entity_id)
            # data-flow skill 增强：从候选的 summary_text 里再抽一波"<table> 表"
            # 图谱可能根本没有 accesses_table 边（PetClinic 就这样），
            # 但 LLM 生成的业务解读里通常会写"查询 vets 表"
            if skill_id == "data-flow":
                summary = (c.get("summary_text") or "") if isinstance(c, dict) else ""
                parsed_tables = self._extract_tables_from_text(summary)
                # 合并去重：图边优先，再补 summary_text 里发现的
                existing_ids = {t["table_id"] for t in base_tables}
                for tid in parsed_tables:
                    if tid not in existing_ids:
                        base_tables.append({"table_id": tid, "operation": "mentioned"})
                        existing_ids.add(tid)
            ctx.table_access_by_entry[entity_id] = base_tables

        return ctx

    # 模块级编译过的正则（编译一次，多次使用）
    # 匹配："<英文标识符>" + 可选空格 + "表"
    # 例如："查询 vets 表" → 命中 "vets"
    #       "写入 visits表" → 命中 "visits"
    # `(?:...)` 是"非捕获组"；这里我们只想拿英文部分，不要"表"字
    # `[A-Za-z_]\w*` 是 Java/Python 风格的标识符（首字符不能是数字）
    _TABLE_MENTION_RE: re.Pattern[str] = re.compile(r"([A-Za-z_]\w*)\s*表")

    @classmethod
    def _extract_tables_from_text(cls, text: str) -> list[str]:
        """从中文 summary_text 抽出"<table> 表"模式里的表名。

        :param text: 业务解读的中文文本
        :return: 去重后的表名列表（保留首次出现顺序）

        示例：
          "该方法 查询 vets 表 并 写入 visits 表"
            → ['vets', 'visits']
        """
        if not text:
            return []
        # `findall` 返回所有匹配的捕获组列表
        # 一个表名可能出现多次，要去重；用 dict.fromkeys 保留首次出现顺序（Python 3.7+ 有序）
        raw = cls._TABLE_MENTION_RE.findall(text)
        return list(dict.fromkeys(raw))

    @staticmethod
    def _rerank_for_business(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """business skill 专用：把 class/module 层级排到 api/method 前。

        实现：稳定 sort —— 给每条记录算一个"业务优先级"，越小越靠前；
        相同优先级保持原顺序（这正是 Python sorted 默认的稳定特性）。

        priority 表：
          class  / module → 0  （业务/能力层，高优先级）
          api    / method → 1  （技术细节层）
          其它             → 2  （未知层级）
        """
        # 用 dict 表达 level → priority 的映射；找不到时默认 2
        _level_priority: dict[str, int] = {
            "class": 0,
            "module": 0,
            "api": 1,
            "method": 1,
        }
        # `key=` 用 lambda 抽提排序键；Python 内置 sorted 是 stable sort（同 key 保持原序）
        return sorted(
            candidates,
            key=lambda c: _level_priority.get((c.get("level") or "").lower(), 2),
        )

    def _bfs_chain(
        self,
        start_id: str,
        *,
        direction: str,
        max_depth: int,
        max_nodes: int,
    ) -> list[str]:
        """对图做有限深度 BFS，返回扁平节点 ID 列表（按发现顺序）。

        :param direction: 'down' 走 successors（callees），'up' 走 predecessors（callers）
        :param max_depth: 最大跳数；1 = 现有行为，2 = dependency 拓展
        :param max_nodes: 列表上限（防爆）

        depth=1 时退化成原来的"取 1 跳邻居前 N 个"，行为完全兼容。
        """
        # 选择遍历方向对应的图 API
        if direction == "down":
            step_fn = self.graph.successors
        elif direction == "up":
            step_fn = self.graph.predecessors
        else:
            return []

        # 经典 BFS：visited 防回环，frontier 当前层节点，result 累计输出
        # 起始节点本身不进 result（result 只装"邻居"，跟原 API 语义一致）
        visited: set[str] = {start_id}
        # `[start_id]` 是初始 frontier；正式开始时它会被替换成第一层邻居
        result: list[str] = []
        frontier: list[str] = [start_id]

        # `range(max_depth)` 跑 max_depth 层
        for _ in range(max_depth):
            next_frontier: list[str] = []
            for node in frontier:
                try:
                    neighbors = list(step_fn(node))
                except Exception:
                    # 图节点不存在 / 后端连不上：当前节点跳过
                    neighbors = []
                for nb in neighbors:
                    if nb in visited:
                        continue
                    visited.add(nb)
                    result.append(nb)
                    next_frontier.append(nb)
                    # 提前截断：达到 max_nodes 就停
                    if len(result) >= max_nodes:
                        return result
            frontier = next_frontier
            if not frontier:
                break  # 没有更深的邻居可走

        return result

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
