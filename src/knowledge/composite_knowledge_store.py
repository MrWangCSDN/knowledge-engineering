"""CompositeKnowledgeStore：BusinessInterpretation + CodeEntity 双源兜底适配器。

设计：[[ReAct-代码层兜底-设计]]

为什么需要：mall-swarm 类工程跑完 P0 pipeline 后 CodeEntity 已有数据（7789 个 1024 维向量 +
Neo4j 图谱）但没跑 with-interpretation，BusinessInterpretation 是空的。
QARetriever 与 ke_search 只查 BI → 空 context → ReAct LLM 投降不调任何工具。

本类包装 (business_store, code_store) 实现 BusinessStoreProto：
  - search_method_hits_by_text：BI 有数据 → 用 BI；BI 空/失败 → 走 CodeEntity 兜底
  - get_by_entity：仅代理 BI（业务解读语义专属，CodeEntity 没对应概念）

设计原则（§5 错误处理）：
  - 永不抛 —— caller 拿 [] 走原有"未找到"路径
  - tenant_not_found / generic error / 返空 —— 三种情况都走兜底
  - code_store 失败 → 返 []（fail-soft）
"""
# `from __future__ import annotations`：类型注解延后求值
# Python 3.10 之前 list[dict] 这类写法在注解里可能报错，这行让 Python 先当字符串处理
from __future__ import annotations

# logging：标准库，提供模块级 logger；比 print 更规范，支持级别过滤
import logging

# typing 模块：提供 Optional、Any、Protocol 等类型注解辅助
# - Optional[X] 等价于 X | None，表示"可以是 X 也可以是 None"
# - Any 表示"任意类型"（宽松，不做类型检查）
# - Protocol 是"结构子类型"接口定义，只要对象有对应方法就算实现（不需要继承）
from typing import Any, Optional, Protocol


# ─── 模块级 logger + 常量 ──────────────────────────────────────────────────

# `__name__` 是当前模块的全限定名（如 "src.knowledge.composite_knowledge_store"）
# 用它命名 logger，方便在 caplog 里按名过滤，也方便生产日志定位来源
_LOG = logging.getLogger(__name__)

# tenant_not_found 异常的标记字串（substring 匹配，避免依赖 weaviate-client 异常类层级）
# Weaviate v4 实测异常消息含 "tenant not found"（mall-swarm staging log）
# 用元组存多个标记，方便之后追加新格式
_TENANT_NOT_FOUND_MARKERS = ("tenant not found", "TenantNotFoundError")


# ─── helper ────────────────────────────────────────────────────────────────


def _is_tenant_missing(exc: Exception) -> bool:
    """判断异常是否表示 Weaviate tenant 不存在。

    Args:
        exc: 捕获到的异常对象
    Returns:
        True 表示 tenant 不存在；False 表示其它异常

    不用 isinstance 是因为 weaviate-client 版本升级时异常类层级常变；
    走 substring 匹配最稳。
    """
    # `str(exc)` 把 exception message 转字符串
    # `.lower()` 把字符串转小写，实现大小写无关的匹配
    msg = str(exc).lower()

    # `any(条件表达式 for 变量 in 可迭代对象)` 是生成器表达式 + any()
    # 等效于: for m in _TENANT_NOT_FOUND_MARKERS: if m.lower() in msg: return True
    # 短路求值：找到第一个 True 就停止，不继续遍历
    return any(m.lower() in msg for m in _TENANT_NOT_FOUND_MARKERS)


# ─── Protocol：让 mock 在测试里能 duck-type ────────────────────────────────


class _BusinessStoreLike(Protocol):
    """business_store 必须有的两个方法 (与 BusinessStoreProto 兼容)。

    Protocol（结构子类型）：Python 3.8 引入，类似 Go 的 interface。
    不需要显式继承，只要对象有这两个方法签名就视为实现了该 Protocol。
    """

    def search_method_hits_by_text(
        self, *, text: str, project_id: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        # `...` 是 Ellipsis，在 Protocol/抽象方法里表示"留给子类实现"
        ...

    def get_by_entity(
        self, entity_id: str, level: Optional[str] = None
    ) -> Optional[dict[str, Any]]:
        ...


class _CodeStoreLike(Protocol):
    """code_store 必须有的方法（与 WeaviateVectorStore.search_by_text 兼容）。"""

    def search_by_text(
        self, query_text: str, top_k: int = 10
    ) -> list[tuple[str, float]]:
        # 返回值是 [(entity_id, score), ...] 列表
        # tuple[str, float] 是 Python 3.9+ 的内置泛型写法
        ...


# ─── 主类 ──────────────────────────────────────────────────────────────────


class CompositeKnowledgeStore:
    """BI + CodeEntity 双源兜底，实现 BusinessStoreProto。

    用法（DI 在 build_retriever_for_project / build_tools_for_project）：

        composite = CompositeKnowledgeStore(
            business_store=biz_adapter,    # WeaviateBusinessAdapter
            code_store=app.state.weaviate_code_store,  # 可为 None
            project_id="mall-swarm",
        )
        retriever = QARetriever(business_store=composite, graph=graph_adapter)
    """

    def __init__(
        self,
        *,                               # `*` 之后的参数都必须用关键字传（keyword-only），防止位置传参混乱
        business_store: _BusinessStoreLike,
        code_store: Optional[_CodeStoreLike],
        project_id: str,
    ):
        """构造：注入 BI / code_store / project_id。

        Args:
            business_store: 主要数据源（BusinessInterpretation adapter）
            code_store: 兜底数据源（CodeEntity vector store）；None 时跳过兜底
            project_id: 当前请求绑定的工程 ID（写入 fallback candidates 用）
        """
        # 按惯例，实例属性以 `_` 开头表示"内部实现，不建议外部直接访问"（约定俗成，非强制）
        self._business_store = business_store
        self._code_store = code_store
        self._project_id = project_id

    # ─── BusinessStoreProto.search_method_hits_by_text ────────────────────

    def search_method_hits_by_text(
        self,
        *,                   # keyword-only 参数
        text: str,
        project_id: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """先 BI，BI 空/失败 → CodeEntity 兜底。

        永不抛：fail-soft 设计，让 caller 拿 [] 走原有"未找到"路径。

        Args:
            text: 查询文本
            project_id: 工程 ID（用于 BI 查询）
            limit: 最多返回条数

        Returns:
            命中列表（每项是 dict），可能为 []
        """
        # 1. 调 BI；catch 所有异常分流
        # `try...except Exception as exc` 捕获所有非系统退出异常
        try:
            bi_hits = self._business_store.search_method_hits_by_text(
                text=text, project_id=project_id, limit=limit
            )
        except Exception as exc:
            # tenant_not_found 是预期分流（INFO 级别，不必告警）
            # 其它异常走 WARNING，让运维知道有异常但不影响用户
            if _is_tenant_missing(exc):
                _LOG.info(
                    "BI tenant 不存在 (project_id=%s)，走 CodeEntity 兜底",
                    project_id,
                )
            else:
                # `%s` 是 logging 的延迟格式化占位符（不用 f-string，性能更好）
                # `type(exc).__name__` 取异常类名（如 "ConnectionError"）
                _LOG.warning(
                    "BI search 失败，走 CodeEntity 兜底 (project_id=%s): %s: %s",
                    project_id, type(exc).__name__, exc,
                )
            # 不管哪种异常，都走 CodeEntity 兜底
            return self._code_fallback(text=text, limit=limit)

        # 2. BI 有数据 → 直接返（truthiness 判断：非空列表为 True）
        if bi_hits:
            return bi_hits

        # 3. BI 返空 → 走兜底
        _LOG.debug(
            "BI 返空 (project_id=%s)，触发 CodeEntity 兜底", project_id
        )
        return self._code_fallback(text=text, limit=limit)

    # ─── BusinessStoreProto.get_by_entity（不做兜底，仅代理） ──────────────

    def get_by_entity(
        self,
        entity_id: str,
        level: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """get_by_entity 仅代理 BI，业务解读语义无 CodeEntity 兜底。

        tenant_not_found / 异常都返 None；不影响 caller。

        Args:
            entity_id: 实体 ID
            level: 可选的层级过滤（method/class/package 等）

        Returns:
            BI 实体 dict，或 None
        """
        try:
            # 尝试用 modern signature（带 project_id kwarg）
            # 新版 WeaviateBusinessAdapter 支持 project_id 参数
            return self._business_store.get_by_entity(
                entity_id, project_id=self._project_id, level=level
            )
        except TypeError:
            # `TypeError` 在参数不匹配时抛出，这里用于兼容旧 signature（无 project_id 参数）
            try:
                return self._business_store.get_by_entity(entity_id, level=level)
            except Exception as exc:
                self._log_get_by_entity_exc(entity_id, exc)
                return None
        except Exception as exc:
            # 其他所有异常（网络、tenant、解析错误等）都静默返 None
            self._log_get_by_entity_exc(entity_id, exc)
            return None

    def _log_get_by_entity_exc(self, entity_id: str, exc: Exception) -> None:
        """get_by_entity 内部异常的日志分流。

        Args:
            entity_id: 出错时的实体 ID（便于定位）
            exc: 捕获到的异常
        """
        if _is_tenant_missing(exc):
            # DEBUG 级别：预期行为，不需要告警
            _LOG.debug(
                "get_by_entity tenant 不存在 (entity_id=%s, project_id=%s)",
                entity_id, self._project_id,
            )
        else:
            # WARNING：非预期，需要关注但不影响业务流程
            _LOG.warning(
                "get_by_entity 失败 (entity_id=%s, project_id=%s): %s: %s",
                entity_id, self._project_id, type(exc).__name__, exc,
            )

    # ─── 私有：CodeEntity 兜底 ────────────────────────────────────────────

    def _code_fallback(self, *, text: str, limit: int) -> list[dict[str, Any]]:
        """从 CodeEntity 向量库取候选，归一化成 BusinessStoreProto 期望的 dict。

        永不抛：code_store 异常 → 返 []。

        Args:
            text: 查询文本
            limit: 最多返回条数

        Returns:
            归一化后的命中列表，每项 {entity_id, summary_text, level}
        """
        # 1. code_store 未注入 → 跳过，直接返空
        if self._code_store is None:
            _LOG.debug("code_store 未注入，跳过 CodeEntity 兜底")
            return []

        # 2. 调 code_store，catch 所有异常实现 fail-soft
        try:
            # WeaviateVectorStore.search_by_text(query_text, top_k) -> [(eid, score), ...]
            # `top_k` 是 WeaviateVectorStore 的参数名，对应我们的 limit
            hits = self._code_store.search_by_text(text, top_k=limit)
        except Exception as exc:
            # code_store 异常不应该阻断整个查询，记录警告后返空
            _LOG.warning(
                "CodeEntity fallback 失败 (project_id=%s): %s: %s",
                self._project_id, type(exc).__name__, exc,
            )
            return []

        # 3. 归一化 + dedup
        # code_store 理论上可能返重复 entity_id（shard 重叠 / query rewrite 等情况），
        # 用 seen set 保留首次出现顺序，避免下游 LLM 看到重复候选
        # `set[str]`：Python 3.9+ 内置泛型写法，集合内只存字符串
        seen: set[str] = set()
        # `list[dict[str, Any]]`：同上，列表内存任意值 dict
        results: list[dict[str, Any]] = []
        # `for (eid, _score) in hits` 是解包赋值：把 tuple 里的两个值分别绑定到变量
        # `_score` 以下划线开头表示"刻意忽略该值"（分数不需要透传给 caller）
        for (eid, _score) in hits:
            # `if x in set` 是 O(1) 查询；list 是 O(N)，所以 set 更适合 dedup
            if eid in seen:
                # 已见过，跳过（保留首次出现顺序）
                continue
            # 标记为已见
            seen.add(eid)
            results.append({
                "entity_id": eid,
                "summary_text": "",       # 实事求是：CodeEntity 没业务解读
                "level": "code_entity",   # 标记兜底来源
                # 不带 name / location —— 节省 token，QARetriever 后续扩展时补
                # 不带 neighbors —— 与 BI 路径同型
            })
        return results
