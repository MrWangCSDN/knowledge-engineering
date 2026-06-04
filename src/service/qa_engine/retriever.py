"""检索阶段：业务概念 → 候选入口方法 + 调用链上下文。

跨仓依赖说明：
  实际的 interpretation_store / graph 实例来自 knowledge-engineering 主仓
  （src/knowledge/weaviate_interpretation_store.py 和 src/knowledge/__init__.py）。
  本仓只用 Protocol 定义"我期望它有什么方法"，运行时 api.py 启动注入实例。
  这样 auth 仓不需要 import 主仓代码，编译/单测可独立。

设计文档：[[首页设计]] §6.1
"""
from __future__ import annotations

# re：Python 标准库正则；这里用来从中文 summary_text 抽"<table> 表"模式
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

# 召回 query 预处理（快赢 A）：剥离渲染指令/口水，仅用于召回向量化
# 设计 [[召回链路缺陷诊断与修复方案]] 快赢 A
from src.service.qa_engine.query_preprocess import clean_recall_query
from src.knowledge.recall_rerank import is_callchain_noise
from src.service.qa_engine.semantic_rerank import should_rerank, rerank_candidates


# ─── source-first grounding P1（[[业务问答-源码优先接地-P1设计]]）───────────────
# 源码预读上限：整方法全文，仅病态超大方法才截断（1M 上下文窗口下常规方法 token 可忽略）。
# 大方法（generateOrder/calcCartPromotion 等 100-150 行）正是 eval 臆造重灾区，给全文才有接地意义。
_SNIPPET_MAX_LINES = 300   # 行数上限
_SNIPPET_MAX_CHARS = 8000  # 字符上限（兜底防超长单行）


def _truncate_snippet(snippet: str, max_lines: int = _SNIPPET_MAX_LINES,
                      max_chars: int = _SNIPPET_MAX_CHARS) -> str:
    """整方法全文返回；仅当超 max_lines 行或 max_chars 字才截断 + 标注。

    标注提示 agent 可调 ke_read_entity 取全文，避免它对截断处的代码尾部臆测。

    Args:
        snippet: 真实源码片段
        max_lines / max_chars: 截断阈值（病态巨型方法兜底）
    Returns:
        原文（常规方法）或「截断片段 + 标注行」（超大方法）
    """
    # str.splitlines()：按行切成列表（不保留行尾换行符）；len 即行数
    lines = snippet.splitlines()
    # 行数与字符数都在上限内 → 常规方法，原样全文返回
    if len(lines) <= max_lines and len(snippet) <= max_chars:
        return snippet
    # 超限：先按行截到 max_lines，再按字符兜底截到 max_chars（防超长单行）
    kept = "\n".join(lines[:max_lines])[:max_chars]
    # 末尾附标注：告知原始行数 + 取全文途径（f-string 内联变量）
    return kept + f"\n…（已截断，原方法共 {len(lines)} 行，调 ke_read_entity 取全文）"


# ─── 结构类型（Protocol）─ 不导入主仓，只定义"接口"────────────────────────

class InterpretationStoreProto(Protocol):
    """跟 src/knowledge/weaviate_interpretation_store.py:WeaviateTopologicalInterpretStore 兼容。

    提供 search_method_hits_by_text（语义检索）和 get_by_entity（精确查询）两个方法。
    """

    def search_method_hits_by_text(
        self, *, text: str, project_id: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        ...

    def get_by_entity(
        self, entity_id: str, level: str | None = None
    ) -> dict[str, Any] | None:
        """按 entity_id 精确查一条拓扑解读；找不到返回 None。"""
        ...


class GraphProto(Protocol):
    """跟 src/knowledge/graph.py:KnowledgeGraph 兼容。"""

    def successors(self, entity_id: str, rel_type: str | None = None) -> list[str]: ...
    def predecessors(self, entity_id: str, rel_type: str | None = None) -> list[str]: ...
    def module_of(self, entity_id: str) -> str | None: ...


# ─── 检索结果数据结构 ──────────────────────────────────────────────────────

@dataclass
class RetrievedContext:
    """喂给 synthesizer 作为 LLM context 的完整资料包。"""

    question: str
    project_id: str
    entry_candidates: list[dict[str, Any]] = field(default_factory=list)
    """语义召回命中（BusinessInterpretation 或 CodeEntity 兜底；含 entity_id / summary_text / level / score）。"""

    callees_by_entry: dict[str, list[str]] = field(default_factory=dict)
    """{ entity_id: [下游 method id, ...] }。仅 top-3 候选取调用链以控制成本。"""

    callers_by_entry: dict[str, list[str]] = field(default_factory=dict)
    """{ entity_id: [上游 caller id, ...] }。"""

    call_edges_by_entry: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    """{ entity_id: [(from_id, to_id), ...] }。入口向下**多跳**展开的调用边（保留父→子关系）。
    C2（[[召回链路缺陷诊断与修复方案]]）：1 跳骨架太空 → LLM 嫌不完整而省略 call_chain；
    故多跳取边喂 LLM 画调用图。仅 top-N 候选展开以控成本。"""

    table_access_by_entry: dict[str, list[dict]] = field(default_factory=dict)
    """{ entity_id: [{table_id, operation}, ...] }。Mode B 需要的数据访问信息。"""

    skill_id: str = "architecture"
    """技能名：召回门控决定——architecture(过线走 KE) 或 chit-chat(低召回)。synthesizer 据此选作答路径。"""

    recall_score: float = 0.0
    """召回门控：top1 相似度（meta/route 事件透传，便于前端显示匹配度 + 调阈值）。"""

    callchain_node_summaries: dict[str, str] = field(default_factory=dict)
    """{ entity_id: 2b中文业务解读(截断) }。逻辑图中文化（[[逻辑图中文化-设计]] §4.1）：
    为 call_edges_by_entry 涉及的真实方法富集 2b 解读，喂 LLM 写准确的中文业务标签。"""

    candidate_code_snippets: dict[str, str] = field(default_factory=dict)
    """{ entity_id: 真实方法源码(整方法全文，仅病态超大截断) }。source-first grounding P1
    （[[业务问答-源码优先接地-P1设计]]）：召回后预读 top-3 候选真实源码注入 prompt，
    治 agent/6段 自由展开代码细节（SQL/表名/字段/存储技术/方法调用/状态码）时的臆造。"""


# ─── 检索器 ─────────────────────────────────────────────────────────────────

class QARetriever:
    """从 Weaviate（语义）+ 图谱（拓扑）取候选实体和调用链。

    召回门控路由（v1.3）：
      - top1 相似度 ≥ recall_threshold → architecture（1 跳 callees + callers）
      - top1 < recall_threshold          → chit-chat（空 ctx，不查图）
    设计文档：[[召回门控路由-设计]]
    """

    # 控制成本：只对 top-N 候选取调用链
    TOP_N_FOR_CHAIN_EXPANSION = 3
    # 控制 context 长度：每个方向只取前 5 个节点
    MAX_CALLEES = 5
    MAX_CALLERS = 5
    # C2：调用链路多跳展开（画流程图用）。深度 2 = 入口→Service→Service 的下游
    # （覆盖下单/退货等主链）；边数上限防 BFS 爆炸 + 控 prompt 体积。
    CHAIN_DEPTH = 2
    MAX_CHAIN_EDGES = 25
    # source-first grounding P1：源码预读只取 top-K 候选（锚定方法几乎总在 top-3；控成本）
    _TOP_K_FOR_SNIPPET = 3

    def __init__(self, *, interpretation_store: InterpretationStoreProto, graph: GraphProto,
                 recall_threshold: float = 0.45):
        # interpretation_store：复合检索源（解读库优先、空/异常兜底 CodeEntity）
        self.interpretation_store = interpretation_store
        # graph：CodeGraph 图导航适配器（GraphProto）
        self.graph = graph
        # 召回门控阈值：top1 相似度 ≥ 它才走 KE，否则闲聊（设计 [[召回门控路由-设计]] §3）
        self.recall_threshold = recall_threshold

    async def retrieve(
        self,
        *,
        question: str,
        project_id: str,
        top_k: int = 5,
    ) -> RetrievedContext:
        """召回门控主入口（设计 [[召回门控路由-设计]] §4）。

        1. 语义召回（带相似度分数）
        2. top1 < recall_threshold → 判闲聊：返回空 ctx，不查图
        3. top1 ≥ recall_threshold → architecture：1 跳上下游 + 表访问(best-effort)
        """
        # 1. 语义召回候选实体（composite：解读库优先，空/异常兜底 CodeEntity；命中带 score）
        # 快赢 A：召回用提纯后的 query（剥离"用流程图展示/是怎么实现的"等渲染指令+口水，
        # 提升代码语义信噪比）；门控 / ctx.question / 作答仍用原始 question，保留用户意图
        recall_text = clean_recall_query(question)
        candidates = self.interpretation_store.search_method_hits_by_text(
            text=recall_text, project_id=project_id, limit=top_k
        )

        # 2. 召回门控：取 top1 相似度
        # c.get("score", 1.0)：CodeEntity 兜底命中带真实分数；解读库命中若无 score 视为 1.0（强信号→通过，设计 §7）
        # max(..., default=0.0)：候选为空时 top1=0.0（必然 < τ → 闲聊）
        top1 = max((c.get("score", 1.0) for c in candidates), default=0.0)

        # top1 没过线 → 判为闲聊：返回空 ctx（不查图、不喂代码），synthesizer 走友好引导
        if top1 < self.recall_threshold:
            return RetrievedContext(
                question=question, project_id=project_id,
                skill_id="chit-chat", recall_score=top1,
            )

        # 3. 过线 → KE(architecture)：装好 ctx，对 top-N 候选取 1 跳上下游 + 表访问
        ctx = RetrievedContext(
            question=question, project_id=project_id,
            skill_id="architecture", recall_score=top1,
        )
        # 召回二次语义重排（[[召回二次重排-设计]]）：门控式护栏——cosine 不自信时才 gte-rerank
        # 重排候选顺序。recall_score 已于上方用原始 cosine top1 算好（门控解耦，不受影响）；
        # 这里只改候选呈现顺序（→ 影响 LLM 候选 + call-chain top-3 入口展开）。用原始 question
        # 而非 recall_text（cross-encoder 吃完整意图）。rerank 内部 best-effort，失败回退原序。
        if should_rerank(candidates):
            candidates = rerank_candidates(question, candidates)
        # entry_candidates 存全量召回结果，synthesizer 可按需截断
        ctx.entry_candidates = candidates

        # 给候选标注所属模块（best-effort）：让 LLM 按 module 判前台/后台，不凭名字臆断（设计 [[模块标签-设计]]）
        # 就地写入 c["module"] 安全：candidates 由 search_method_hits_by_text 每次新建，调用方不持有别名
        # 遍历 entry_candidates 列表，c 是单个候选字典（dict），就地写入 "module" 键
        for c in ctx.entry_candidates:
            # c.get("entity_id")：安全取 entity_id，字典里无此键时返 None（比 c["entity_id"] 不抛异常）
            eid = c.get("entity_id")
            try:
                # graph.module_of：CodeGraph file_path 顶层目录（mall-portal/mall-admin/...）；查不到→None
                # 三元表达式 `A if B else C`：B 为真时取 A，否则取 C；此处 eid 为 None/空串→跳过 module_of 调用
                c["module"] = self.graph.module_of(eid) if eid else None
            except Exception:
                # 单个候选查模块失败不影响其余候选与主检索；写 None 占位，保持字段存在
                c["module"] = None

        # source-first grounding（P1，[[业务问答-源码优先接地-P1设计]]）：预读 top-3 候选真实源码注入，
        # 让 synthesizer 答代码细节（SQL/字段/存储/状态码）时有一手料、不臆造。
        # 候选已在上方定稿（rerank + entry_candidates 赋值），本步只 enrich、不影响门控/排序。
        self._enrich_candidate_snippets(ctx)

        # 只对 top-N 候选取调用链（控成本）
        for c in candidates[: self.TOP_N_FOR_CHAIN_EXPANSION]:
            entity_id = c.get("entity_id")
            if not entity_id:
                continue
            # 向下（callees）/ 向上（callers）各 1 跳
            ctx.callees_by_entry[entity_id] = self._bfs_chain(
                entity_id, direction="down", max_depth=1, max_nodes=self.MAX_CALLEES
            )
            ctx.callers_by_entry[entity_id] = self._bfs_chain(
                entity_id, direction="up", max_depth=1, max_nodes=self.MAX_CALLERS
            )
            # 数据表访问（best-effort；CodeGraph 无 accesses_table 边时返 []）
            ctx.table_access_by_entry[entity_id] = self._extract_table_access(entity_id)
            # C2：入口向下多跳展开调用边（保留 from→to），供 LLM 画 call_chain 调用图
            ctx.call_edges_by_entry[entity_id] = self._bfs_edges(
                entity_id, max_depth=self.CHAIN_DEPTH, max_edges=self.MAX_CHAIN_EDGES
            )

        # 逻辑图中文化（[[逻辑图中文化-设计]] §4.1）：为调用链涉及的真实方法批量查 2b 中文业务解读，
        # 喂给 LLM 写准确的中文业务标签（A1 锚定式）。职责：retriever 管召回+富集，synthesizer 不碰 store。
        # 端点集 = call_edges_by_entry 所有边的去重 from/to（= 调用链上全部真实方法）。
        recalled_ids: set[str] = set()
        # dict.values() 是各入口的边列表；每条边是 (from_id, to_id) 二元组
        for edges in ctx.call_edges_by_entry.values():
            for frm, to in edges:
                recalled_ids.add(frm)
                recalled_ids.add(to)
        # 逐个查 2b 解读：composite.get_by_entity(entity_id, level=None) 永不抛、取不到返 None。
        # 规模 ~10-25 节点/次，循环单查可接受（批量接口列为后续优化，YAGNI）。
        for mid in recalled_ids:
            try:
                # interpretation_store 是 composite（内部按 self._project_id 绑 tenant）
                rec = self.interpretation_store.get_by_entity(mid)
                # id 带参查不到 → 剥参再试：2b 解读 id 多为无参 Class::method 形态，而调用边 id 常带
                # 参数签名（Class::method#(params)）→ 不剥参会漏匹配大量解读（实测中文覆盖 3/11 的根因）
                if not rec and "#" in mid:
                    rec = self.interpretation_store.get_by_entity(mid.split("#", 1)[0])
            except Exception:
                # best-effort 富集：单节点查询异常吞掉，不阻断整体召回
                rec = None
            if not rec:
                continue
            # 兼容多种返回字段名（解读库列 interpretation_text/context_summary；适配器映射 summary_text）
            text = (
                rec.get("summary_text")
                or rec.get("interpretation_text")
                or rec.get("context_summary")
                or ""
            ).strip()
            if text:
                # 截断控 token（设计 §9：每条 ≤120 字）
                ctx.callchain_node_summaries[mid] = text[:120]
        return ctx

    def _enrich_candidate_snippets(self, ctx: RetrievedContext) -> None:
        """给 top-3 候选预读真实方法源码，写入 ctx.candidate_code_snippets（source-first grounding P1）。

        best-effort 设计：
        - interpretation_store(composite) 无 get_code_snippet（旧实例）→ 整体跳过、不崩；
        - 单候选取不到 / None / 异常 → 跳过该候选；
        - 永不抛、不改候选顺序与召回门控（候选已在 retrieve 上方定稿）。

        Args:
            ctx: 召回上下文；就地写入 ctx.candidate_code_snippets[entity_id] = 截断后源码
        """
        # getattr 探测 composite 是否提供 get_code_snippet（旧实例/未升级时无此方法）；缺省 None
        getter = getattr(self.interpretation_store, "get_code_snippet", None)
        # callable 校验：拿到的不是可调用对象（如 None）→ 整体跳过预读
        if not callable(getter):
            return
        # 切片只取 top-K 候选（list[:k] 不越界，长度不足时返回全部）
        for c in ctx.entry_candidates[: self._TOP_K_FOR_SNIPPET]:
            eid = c.get("entity_id")    # 安全取 entity_id，缺键返 None
            if not eid:                 # 无 id 的脏候选跳过
                continue
            try:
                snippet = getter(eid)   # composite.get_code_snippet 已 fail-soft（内部不抛）
            except Exception:
                # 双保险：即便 getter 实现异常也不打断召回主流程
                snippet = None
            if not snippet:             # None / 空串 → 该候选无源码，退回仅靠 2b 解读
                continue
            # 整方法全文写入；仅病态超大方法被 _truncate_snippet 截断 + 标注
            ctx.candidate_code_snippets[eid] = _truncate_snippet(snippet)

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
        :param max_depth: 最大跳数；当前调用方固定传 1（1 跳邻居），方法本身支持多跳
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

    def _bfs_edges(
        self,
        start_id: str,
        *,
        max_depth: int,
        max_edges: int,
    ) -> list[tuple[str, str]]:
        """对图向下做有限深度 BFS，返回 (父, 子) 调用边列表（**保留层级关系**）。

        与 _bfs_chain（返回扁平节点列表）的区别：本方法保边，让 LLM 能画出正确的多跳
        调用图——不会把 2 跳节点误当成入口的直接 callee（C2 [[召回链路缺陷诊断与修复方案]]）。

        :param start_id: 入口实体 id
        :param max_depth: 最大跳数（C2 用 2：入口→直接 callee→再下一跳）
        :param max_edges: 边数上限（防 BFS 爆炸 + 控 prompt 体积）
        :return: [(from_id, to_id), ...]，按 BFS 发现顺序；可能含指向已访问节点的横向边
        """
        edges: list[tuple[str, str]] = []
        # visited 防止重复**展开**同一节点（横向边仍记录，保留图结构）
        visited: set[str] = {start_id}
        frontier: list[str] = [start_id]

        # range(max_depth) 跑 max_depth 层；与 _bfs_chain 同构，额外记录父→子边
        for _ in range(max_depth):
            next_frontier: list[str] = []
            for node in frontier:
                try:
                    children = list(self.graph.successors(node))
                except Exception:
                    # 节点查不到 / 图后端异常：跳过该节点，不中断整体
                    children = []
                for child in children:
                    # 降噪：getter/setter、MyBatis Example/CRUD、结果包装类等是调用图噪声，
                    # 直接跳过——既不画进图，也不消耗有限的 max_edges 预算（把预算留给业务调用，
                    # 否则 ServiceImpl 一上来调一串 setter 就把额度吃光，真业务调用反被截断）。
                    if is_callchain_noise(child):
                        continue
                    edges.append((node, child))      # 保边：node → child
                    if len(edges) >= max_edges:
                        return edges                 # 达边数上限即停（防爆）
                    if child not in visited:
                        visited.add(child)
                        next_frontier.append(child)  # 只展开未访问过的子节点
            frontier = next_frontier
            if not frontier:
                break                                # 没有更深的节点可走
        return edges

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
