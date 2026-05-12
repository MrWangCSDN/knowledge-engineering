"""真实 retriever 适配器 —— 把主仓的 Weaviate / Neo4j 客户端，封装成
QARetriever Protocol 期望的接口形状。

设计要点（[[首页设计]] §6.1）：

1. **不改主仓代码**：主仓的 `WeaviateBusinessInterpretStore` 和 `Neo4jGraphBackend`
   一字未动；这里只做"翻译"。

2. **多层级检索**：原 `search_method_hits_by_text` 仅返回 level=api 的 method,
   不够喂"整体架构 / 业务规则"类问题。本 adapter 改成对 BusinessInterpretation
   做全库 near_vector，覆盖 method / class / module / api 各层。

3. **v2.0 Native Multi-Tenancy**：使用 Weaviate `with_tenant(project_id)` 把每个
   project 的数据物理隔离到独立分区。project_id 不再只是日志标注，而是真正的
   tenant 绑定。schema 层面要求 Weaviate collection 开启 Multi-Tenancy。

4. **get_by_entity + project_id**：Task 20 新增 project_id 必填参数；
   主仓 WeaviateBusinessInterpretStore.get_by_entity 暂未接受 tenant 参数
   （Task 23 才改主仓），这里用 try/except 兜底：先尝试带 tenant 的私有方法，
   失败则 fallback 到原来的 get_by_entity（TODO 标注）。

类型注解中的 `list[dict[str, Any]]`：用 `Any` 是因为 Weaviate 返回的 properties
是异构 dict，强类型化收益不大。
"""
from __future__ import annotations

# 导入 logging：标准日志模块，比 print 多了级别 / handler / 路径信息
import logging
# typing 模块：给静态类型检查器看的，运行时不影响
from typing import Any, Optional

# 主仓的业务解读 store；通过 PYTHONPATH 拉到（两个仓是 worktree 同根，src 路径一致）
from src.knowledge.weaviate_business_store import WeaviateBusinessInterpretStore
# 主仓的 Neo4j 后端（带 successors / predecessors 完整 API）
from src.knowledge.graph_neo4j import Neo4jGraphBackend

# 模块级 logger：用模块名作为 logger 名是 Python 惯例（便于过滤）
_log = logging.getLogger(__name__)


# ─── 业务解读 adapter ────────────────────────────────────────────────────────

class WeaviateBusinessAdapter:
    """实现 `qa_engine.retriever.BusinessStoreProto`。

    内部就是包一层 `WeaviateBusinessInterpretStore`，但把检索结果
    从 list[(method_id, score)] 翻译成 QARetriever 期望的 list[dict]，
    并且不强制 level=api，支持跨层级问答。
    """

    def __init__(self, store: WeaviateBusinessInterpretStore) -> None:
        """构造函数：注入已经连好的 store。

        :param store: 主仓的 `WeaviateBusinessInterpretStore` 实例（已 connect）
        """
        # 把外部传入的 store 存到实例属性；下划线开头表示"模块内部使用"
        self._store = store

    def get_by_entity(
        self,
        entity_id: str,
        *,
        project_id: str,
        level: str | None = None,
    ) -> dict[str, Any] | None:
        """按 entity_id 精确查一条业务解读；找不到返回 None。

        v2.0：新增必填 project_id，绑定 Weaviate tenant 做数据隔离。
        主仓 WeaviateBusinessInterpretStore 的 get_by_entity_with_tenant 方法由
        Task 23 添加；这里先用 try/except 兜底，失败时 fallback 到无 tenant 版本。

        :param entity_id: 形如 'method//xxx' / 'class//xxx' / 'module//xxx'
        :param project_id: Weaviate tenant 标识（= 项目 ID）
        :param level: 可选过滤 'api'/'class'/'module' 等
        """
        try:
            # v2.0 路径：调用主仓带 tenant 的方法（Task 23 会在主仓添加此方法）
            # TODO(Task 23): 主仓添加 get_by_entity_with_tenant 后移除 fallback
            return self._store.get_by_entity_with_tenant(
                entity_id, tenant=project_id, level=level
            )
        except AttributeError:
            # 主仓还没有 get_by_entity_with_tenant（Task 23 前的兜底）
            # fallback 到无 tenant 版本；数据隔离由上层逻辑保证
            _log.debug(
                "get_by_entity_with_tenant 未实现，fallback 到无 tenant 版本"
                " entity_id=%s project_id=%s",
                entity_id, project_id,
            )
            try:
                return self._store.get_by_entity(entity_id, level=level)
            except Exception as e:
                _log.warning("get_by_entity fallback 失败 entity_id=%s: %s", entity_id, e)
                return None
        except Exception as e:
            # 任何其他异常都不抛；让上层（tool / retriever）决定怎么处理
            _log.warning("get_by_entity 失败 entity_id=%s project_id=%s: %s", entity_id, project_id, e)
            return None

    def search_method_hits_by_text(
        self,
        *,
        text: str,
        project_id: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """语义检索 BusinessInterpretation collection。

        v2.0：使用 Weaviate Native Multi-Tenancy，通过 with_tenant(project_id)
        把查询限定在 project_id 对应的 tenant 分区内，实现多工程数据物理隔离。

        匹配 `BusinessStoreProto` 的接口签名（命名参数 + 完全相同的 kwargs）。

        :param text: 用户的问题原文
        :param project_id: 必填，作为 Weaviate tenant 标识（= 项目 ID）；
                           空字符串时直接返回 []，防止跨 tenant 误查
        :param limit: 返回最多多少条候选
        :return: list[dict]，每条含 entity_id / summary_text / level / business_domain
                 / business_capabilities / entity_type / language
        """
        # 边界保护：空问题 → 直接返回 []
        # 空 project_id 也保护：避免 with_tenant("") 查到跨租户数据或报错
        if not (text or "").strip() or not project_id:
            return []

        # v2.0 日志：记录 tenant 绑定信息，便于追踪多租户隔离问题
        _log.debug(
            "search_method_hits_by_text: tenant(project_id)=%s, text=%r",
            project_id, text[:60],
        )

        # 调用主仓的私有方法做近邻搜索；这里复用 store 的内部组件以减少重复
        # 类型注解 `Optional[list[...]]` = `list[...] | None`，表示可能返回 None
        try:
            # 1) 拿 embedding：主仓的 `get_embedding` 走 Ollama bge-m3
            from src.semantic.embedding import get_embedding
            # 2) 近邻向量检索的封装：返回 (properties_dict, score) 列表
            from src.knowledge.weaviate_near_vector import near_vector_property_hits

            # 向量化 query 文本；维度由 store 的初始化参数决定（PetClinic 用 1024）
            vec = get_embedding(text.strip(), self._store._dim)

            # v2.0 Native Multi-Tenancy：
            # _get_collection() 返回未绑定 tenant 的 Collection 对象
            # with_tenant(project_id) 返回一个"只看 project_id 分区"的 TenantView
            # 之后所有查询（near_vector / get / filter）都在这个分区内执行
            coll = self._store._get_collection().with_tenant(project_id)

            # 多取一些再筛：因为 LLM 喂 5-10 条就够了，给 limit*3 的查询余量
            # 例如 limit=5 时取 15 条
            fetch_limit = max(int(limit) * 3, 10)

            # 关键调用：near_vector + 返回所有字段（包括 summary_text 用于喂 LLM）
            # coll 已经是 tenant-scoped 视图，不需要额外 filter project_id
            rows = near_vector_property_hits(
                coll,
                vector=vec,
                dim=self._store._dim,
                limit=fetch_limit,
                collection_name=self._store._collection_name,
                return_properties=[
                    "entity_id",
                    "entity_type",
                    "level",
                    "summary_text",
                    "business_domain",
                    "business_capabilities",
                    "language",
                ],
                # tenant 分区已经做了隔离，不再需要额外的 project_id filter
                filters=None,
            )
        except Exception as e:
            # 任何异常（向量化失败 / 网络断开 / Weaviate 连不上 / tenant 不存在）都不上抛
            # 上层 QARetriever 会基于空 candidates 走"未找到"分支
            _log.warning("Weaviate 业务解读检索失败 project_id=%s: %s", project_id, e)
            return []

        # 排序：score 从高到低，截断到 limit
        # rows 元素是 (props_dict, score_float)；用 lambda 取第 [1] 个元素当 key
        rows.sort(key=lambda r: r[1], reverse=True)

        # 翻译成 QARetriever 期望的格式
        out: list[dict[str, Any]] = []
        for props, score in rows[: int(limit)]:
            # `props.get(k, "")` 是字典的"取值带默认"语法；找不到 k 时返回 ""
            out.append(
                {
                    "entity_id": props.get("entity_id", ""),
                    "entity_type": props.get("entity_type", ""),
                    "level": props.get("level", ""),
                    "summary_text": props.get("summary_text", ""),
                    "business_domain": props.get("business_domain", ""),
                    "business_capabilities": props.get("business_capabilities", ""),
                    "language": props.get("language", ""),
                    # 把相似度也带出来，方便上层调试；不在 Protocol 里但 dict 允许多余字段
                    "score": float(score),
                }
            )

        return out


# ─── 图谱 adapter ────────────────────────────────────────────────────────────

class Neo4jGraphAdapter:
    """实现 `qa_engine.retriever.GraphProto`。

    v2.0：project_id 现在是必填参数，所有 Cypher 查询都带 project_id WHERE
    条件，实现多工程数据隔离。不再通过主仓 Neo4jGraphBackend 的 successors /
    predecessors 方法（它们不知道 project_id），而是直接用 backend._driver
    发送带隔离过滤的 Cypher。

    单独定义类的好处：
      1. 给 Protocol 一个具名实现，方便后续扩展（加 caching / 过滤 / 重试）
      2. 在依赖注入处 (api.py) 看着对称：BusinessAdapter / GraphAdapter
      3. 关闭时统一释放（Neo4jGraphBackend.close 不会自动关）
    """

    def __init__(self, backend: Neo4jGraphBackend, project_id: str) -> None:
        """v2.0：project_id 现在是必填，绑定单工程上下文。

        :param backend: 主仓已初始化好的 Neo4jGraphBackend（含 driver）
        :param project_id: 工程标识，非空字符串；空值直接拒绝（防止跨租户误查）
        :raises TypeError: project_id 缺失或为空时抛出
        """
        # 空值检查：None 或 "" 都被拒绝，强制调用方显式传 project_id
        if not project_id:
            raise TypeError("project_id is required for Neo4jGraphAdapter (v2.0)")
        # 注入已经初始化好的 Neo4j 后端
        self._backend = backend
        # 保存 project_id，后续所有查询都会带上这个过滤条件
        self._project_id = project_id

    def successors(self, entity_id: str, rel_type: Optional[str] = None) -> list[str]:
        """下游邻居（method A --calls--> method B 时，A 的 successors 含 B）。

        v2.0：直接走 driver 发送带 project_id 的 Cypher，确保只返回
        同一 project 内的节点，不再委托 backend.successors。
        """
        # 用 backend._driver.session() 打开一个会话（上下文管理器，自动 close）
        # with ... as s: 是 Python 上下文管理器语法，等效于 try/finally close()
        try:
            with self._backend._driver.session() as s:
                # Cypher：找到 entity_id + project_id 匹配的起始节点，
                # 再沿任意关系 [r]-> 找到同 project 的下游节点 b
                # $eid / $pid / $rel 是 Cypher 参数占位符，防注入且可复用计划缓存
                result = s.run(
                    """
                    MATCH (a:Entity {id: $eid, project_id: $pid})-[r]->(b:Entity)
                    WHERE b.project_id = $pid
                      AND ($rel = '' OR type(r) = $rel)
                    RETURN b.id AS nid
                    """,
                    eid=entity_id,
                    pid=self._project_id,
                    rel=rel_type or "",  # None 转成 '' 与 Cypher 条件对应
                )
                # 把每行的 "nid" 字段取出，组成 list[str] 返回
                return [row["nid"] for row in result]
        except Exception as e:
            _log.debug("Neo4j successors(%s, %s) 失败: %s", entity_id, rel_type, e)
            return []

    def predecessors(self, entity_id: str, rel_type: Optional[str] = None) -> list[str]:
        """上游邻居（调用方）。

        v2.0：与 successors 对称，方向改为 <-[r]-（即找"谁调用了 entity_id"）。
        同样带 project_id 过滤，防止跨工程返回节点。
        """
        try:
            with self._backend._driver.session() as s:
                # Cypher：方向反转 <-[r]-，找上游节点 b
                result = s.run(
                    """
                    MATCH (a:Entity {id: $eid, project_id: $pid})<-[r]-(b:Entity)
                    WHERE b.project_id = $pid
                      AND ($rel = '' OR type(r) = $rel)
                    RETURN b.id AS nid
                    """,
                    eid=entity_id,
                    pid=self._project_id,
                    rel=rel_type or "",
                )
                return [row["nid"] for row in result]
        except Exception as e:
            _log.debug("Neo4j predecessors(%s, %s) 失败: %s", entity_id, rel_type, e)
            return []

    def close(self) -> None:
        """显式释放：关闭 Neo4j driver，进程退出前调用。

        FastAPI 没有官方的 shutdown 钩子用 backend.close，
        api.py 在 @app.on_event('shutdown') 会调用这里。
        """
        try:
            self._backend.close()
        except Exception:
            # 关闭失败不影响进程退出
            pass
