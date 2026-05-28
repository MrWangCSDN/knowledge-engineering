# ReAct 代码层兜底 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 mall-swarm 类无业务解读数据的工程也能跑 ReAct。新增 CompositeKnowledgeStore 在 BI 空/失败时兜底用 CodeEntity 向量库；改 AGENT_SYSTEM_PROMPT 引导 LLM 主动调工具探索而不是直接放弃。

**Architecture:** 1) 新增 `src/knowledge/composite_knowledge_store.py`：实现 `BusinessStoreProto`，包装 BI adapter + CodeEntity vector store；`search_method_hits_by_text` 先调 BI，空/抛 `tenant not found` 时走 `_code_fallback`（CodeEntity 语义检索 → Neo4j 取 attrs → 归一化 `level="code_entity"`）；`get_by_entity` 仅代理 BI。2) `build_retriever_for_project` / `build_tools_for_project` 注入 Composite 替代 BI adapter。3) AGENT_SYSTEM_PROMPT 删"context 不足直接说未找到"条 + 加"探索流程"段。`QARetriever` / `ke_search` / 其他 11 工具 / 前端零改。

**Tech Stack:** Python 3.12 / pytest / unittest.mock / weaviate-client v4（现有依赖）。

**仓库 / 分支:** `/Users/java/knowledge-engineering-auth` 分支 `release-0513`（继续本会话长期分支，无 worktree）。

**Spec 来源:** Obsidian [[ReAct-代码层兜底-设计]]（已批准，2026-05-28）。

**关键背景**：
- `BusinessStoreProto` 接口（`src/service/qa_engine/retriever.py:13`）：`search_method_hits_by_text(*, text, project_id, limit=5)` + `get_by_entity(entity_id, level=None)`
- `WeaviateVectorStore.search_by_text(query_text, top_k=10) -> list[tuple[str, float]]`（`src/knowledge/vector_store_weaviate.py:231`，内部 `get_embedding` 再 `search_by_vector`）
- `Neo4jGraphAdapter.successors / predecessors(entity_id, rel_type=None) -> list[str]`（`src/service/qa_engine/adapters.py:235/265`，自动 project_id 过滤）
- `Neo4jGraphAdapter` 没有暴露 `get_node`；要拿 entity 的 name / location attrs 走 `neo4j_backend.get_node` 间接读（也可只返 entity_id + 让 QARetriever 后续扩展时填）
- `WeaviateBusinessAdapter.search_method_hits_by_text` 已返 list[dict]（含 entity_id / summary_text / level / ...）
- `app.state.weaviate_code_store` 在 `_try_connect_backends` 已连，可选；为 None 时跳过 fallback
- AGENT_SYSTEM_PROMPT 在 `src/service/qa_engine/prompts.py:137`

**Run tests:** `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth tests/test_knowledge -q`

---

## File Structure

| 文件 | 改动 | 责任 |
|---|---|---|
| `src/knowledge/composite_knowledge_store.py` | 🆕 ~180 行 | CompositeKnowledgeStore 类 + `_is_tenant_missing` helper |
| `tests/test_knowledge/__init__.py` | 🆕 空 | pytest 包识别 |
| `tests/test_knowledge/test_composite_knowledge_store.py` | 🆕 ~280 行 | 10 单测（7 search + 3 get_by_entity） |
| `src/service/qa_router.py` | Modify | `build_retriever_for_project` + `build_tools_for_project` 注入 Composite（约 15 行） |
| `src/service/qa_engine/prompts.py` | Modify | AGENT_SYSTEM_PROMPT 删 1 条 + 加 1 段（约 30 行） |
| `src/service/qa_engine/react_synthesizer.py` | Modify | `_build_tool_usage_hint` 第 4 条加 1 行说明 |
| `tests/test_auth/test_qa_react_synthesizer.py` | Modify | 追加 2 个 prompt 校验测试 |

---

## Task 1: CompositeKnowledgeStore 主体 + search_method_hits_by_text 7 单测

**Files:**
- Create: `tests/test_knowledge/__init__.py`
- Create: `tests/test_knowledge/test_composite_knowledge_store.py`
- Create: `src/knowledge/composite_knowledge_store.py`

- [ ] **Step 1: 创建 tests/test_knowledge 包**

```bash
mkdir -p /Users/java/knowledge-engineering-auth/tests/test_knowledge
touch /Users/java/knowledge-engineering-auth/tests/test_knowledge/__init__.py
```

- [ ] **Step 2: 写 7 个失败测试 `tests/test_knowledge/test_composite_knowledge_store.py`**

```python
"""CompositeKnowledgeStore 单测 — mock BI / code store / graph，不连真后端。

设计：[[ReAct-代码层兜底-设计]] §6
"""
# unittest.mock：标准库，假对象工具
from unittest.mock import MagicMock

# pytest：测试框架；用 fixture / raises 等
import pytest

# 被测：first run 会 ImportError —— TDD RED 阶段
from src.knowledge.composite_knowledge_store import (
    CompositeKnowledgeStore,
    _is_tenant_missing,
)


# ─── fixture: 通用构造 ────────────────────────────────────────────────────


def _make_composite(
    bi_results=None,        # business_store.search_method_hits_by_text 的返回值
    bi_exc=None,            # business_store.search_method_hits_by_text 抛的异常（覆盖 bi_results）
    code_results=None,      # code_store.search_by_text 的返回值（[(eid, score), ...]）
    code_exc=None,
    has_code=True,          # 是否注入 code_store；False 模拟未连
):
    """造一个 CompositeKnowledgeStore + mock 子组件，返回 (composite, bi_mock, code_mock)。"""
    # bi 是 BusinessStoreProto 兼容 mock（search_method_hits_by_text + get_by_entity）
    bi = MagicMock()
    if bi_exc is not None:
        # `side_effect` 设为异常实例时，调用时会 raise 该异常
        bi.search_method_hits_by_text.side_effect = bi_exc
    else:
        bi.search_method_hits_by_text.return_value = bi_results or []

    # code_store mock（search_by_text 返回 [(entity_id, score), ...]）
    if not has_code:
        code = None
    else:
        code = MagicMock()
        if code_exc is not None:
            code.search_by_text.side_effect = code_exc
        else:
            code.search_by_text.return_value = code_results or []

    composite = CompositeKnowledgeStore(
        business_store=bi,
        code_store=code,
        project_id="mall-swarm",
    )
    return composite, bi, code


# ─── _is_tenant_missing helper 测试 ────────────────────────────────────────


def test_is_tenant_missing_recognizes_lowercase():
    """'tenant not found' 任意大小写都能识别。"""
    assert _is_tenant_missing(RuntimeError("tenant not found: mall-swarm")) is True
    assert _is_tenant_missing(RuntimeError("Tenant Not Found")) is True
    assert _is_tenant_missing(RuntimeError("TenantNotFoundError xxx")) is True


def test_is_tenant_missing_returns_false_for_unrelated_errors():
    """非 tenant 相关的异常应当返 False（让 caller 走 generic 分支）。"""
    assert _is_tenant_missing(ValueError("bad input")) is False
    assert _is_tenant_missing(ConnectionError("network down")) is False


# ─── search_method_hits_by_text 7 个测试 ───────────────────────────────────


def test_bi_has_results_returns_bi_directly():
    """BI 有 3 个命中 → 直接返；不调 code_store。"""
    # BI 返 3 条命中（mock 数据，字段不全也无所谓 — 此处只测路径）
    bi_hits = [
        {"entity_id": "method//a", "summary_text": "处理订单", "level": "method"},
        {"entity_id": "method//b", "summary_text": "查商品", "level": "method"},
        {"entity_id": "class//c", "summary_text": "用户模块", "level": "class"},
    ]
    composite, bi, code = _make_composite(bi_results=bi_hits)

    result = composite.search_method_hits_by_text(
        text="订单怎么处理", project_id="mall-swarm", limit=5
    )

    assert result == bi_hits  # 原样返
    # BI 被调一次；code_store 完全没被调用
    bi.search_method_hits_by_text.assert_called_once()
    code.search_by_text.assert_not_called()


def test_bi_returns_empty_falls_to_code_entity():
    """BI 返 [] → 走 _code_fallback，从 code_store 拿候选。"""
    code_hits = [
        ("method//11cd3f041163", 0.87),
        ("method//a8e3f1f41a55d734", 0.84),
    ]
    composite, bi, code = _make_composite(bi_results=[], code_results=code_hits)

    result = composite.search_method_hits_by_text(
        text="getMenuList 怎么用", project_id="mall-swarm", limit=5
    )

    assert len(result) == 2
    # code_store.search_by_text 被调一次，参数是 text + limit
    code.search_by_text.assert_called_once_with("getMenuList 怎么用", top_k=5)
    # entity_id 透传
    assert result[0]["entity_id"] == "method//11cd3f041163"
    assert result[1]["entity_id"] == "method//a8e3f1f41a55d734"


def test_bi_raises_tenant_not_found_falls_to_code():
    """BI 抛 `tenant not found` → catch + 走 fallback。"""
    code_hits = [("method//x", 0.9)]
    composite, bi, code = _make_composite(
        bi_exc=RuntimeError("tenant not found: mall-swarm"),
        code_results=code_hits,
    )

    result = composite.search_method_hits_by_text(
        text="anything", project_id="mall-swarm", limit=5
    )

    assert len(result) == 1
    assert result[0]["entity_id"] == "method//x"
    code.search_by_text.assert_called_once()


def test_bi_raises_generic_error_falls_to_code_warns(caplog):
    """BI 抛 generic Exception → WARNING log + 走 fallback。

    `caplog` 是 pytest 内建 fixture，捕获 logging 输出供断言。
    """
    code_hits = [("method//y", 0.8)]
    composite, bi, code = _make_composite(
        bi_exc=ConnectionError("network down"),
        code_results=code_hits,
    )

    # `caplog.at_level(level, logger_name)` 设捕获级别和 logger
    import logging
    with caplog.at_level(logging.WARNING, logger="src.knowledge.composite_knowledge_store"):
        result = composite.search_method_hits_by_text(
            text="x", project_id="mall-swarm", limit=5
        )

    assert len(result) == 1
    # 验证日志：含 generic error 提示
    assert any("BI" in rec.message and "ConnectionError" in rec.message for rec in caplog.records)


def test_code_fallback_normalizes_to_canonical_shape():
    """code_store 返 (eid, score) tuple → 归一化为 dict，level='code_entity'，summary_text=''。"""
    code_hits = [
        ("method//abc", 0.95),
        ("class//xyz", 0.78),
    ]
    composite, bi, code = _make_composite(bi_results=[], code_results=code_hits)

    result = composite.search_method_hits_by_text(
        text="x", project_id="mall-swarm", limit=5
    )

    assert len(result) == 2
    # 字段规范：entity_id / summary_text / level
    for item in result:
        assert "entity_id" in item
        assert item["summary_text"] == ""      # 实事求是，无业务解读
        assert item["level"] == "code_entity"  # 标记是代码层兜底
    assert result[0]["entity_id"] == "method//abc"
    assert result[1]["entity_id"] == "class//xyz"
    # 不带 neighbors（QARetriever 后置扩展）
    assert "neighbors" not in result[0]


def test_code_store_raises_returns_empty(caplog):
    """code_store 抛 → 返 [] 给 caller，记 WARNING。"""
    composite, bi, code = _make_composite(
        bi_results=[],
        code_exc=RuntimeError("code store unavailable"),
    )

    import logging
    with caplog.at_level(logging.WARNING, logger="src.knowledge.composite_knowledge_store"):
        result = composite.search_method_hits_by_text(
            text="x", project_id="mall-swarm", limit=5
        )

    assert result == []
    assert any("CodeEntity fallback 失败" in rec.message for rec in caplog.records)


def test_code_store_none_skips_fallback():
    """composite 构造时 code_store=None → BI 空直接返 []，不尝试 fallback。"""
    composite, bi, code = _make_composite(bi_results=[], has_code=False)

    result = composite.search_method_hits_by_text(
        text="x", project_id="mall-swarm", limit=5
    )

    assert result == []
    # bi 被调；code 是 None 没法被调
    bi.search_method_hits_by_text.assert_called_once()
```

- [ ] **Step 3: 跑测试 expect 9 FAIL（模块不存在）**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_knowledge/test_composite_knowledge_store.py -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'src.knowledge.composite_knowledge_store'` —— RED 阶段。

- [ ] **Step 4: 创建 `src/knowledge/composite_knowledge_store.py`**

```python
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
from __future__ import annotations

# logging：模块级 logger，让 WARN/INFO/DEBUG 经标准日志通道
import logging
# typing：Optional[X] = X | None；Protocol-friendly
from typing import Any, Optional, Protocol


# ─── 模块级 logger + 常量 ──────────────────────────────────────────────────

_LOG = logging.getLogger(__name__)

# tenant_not_found 异常的标记字串（substring 匹配，避免依赖 weaviate-client 异常类层级）
# Weaviate v4 实测异常消息含 "tenant not found"（mall-swarm staging log）
_TENANT_NOT_FOUND_MARKERS = ("tenant not found", "TenantNotFoundError")


# ─── helper ────────────────────────────────────────────────────────────────


def _is_tenant_missing(exc: Exception) -> bool:
    """判断异常是否表示 Weaviate tenant 不存在。

    不用 isinstance 是因为 weaviate-client 版本升级时异常类层级常变；
    走 substring 匹配最稳。
    """
    # `str(exc)` 把 exception message 转字符串；lower() 让大小写无关
    msg = str(exc).lower()
    # `any(... for ... in ...)` 是生成器表达式，短路求值
    return any(m.lower() in msg for m in _TENANT_NOT_FOUND_MARKERS)


# ─── Protocol：让 mock 在测试里能 duck-type ────────────────────────────────


class _BusinessStoreLike(Protocol):
    """business_store 必须有的两个方法 (与 BusinessStoreProto 兼容)。"""
    def search_method_hits_by_text(self, *, text: str, project_id: str, limit: int = 5) -> list[dict[str, Any]]:
        ...
    def get_by_entity(self, entity_id: str, level: Optional[str] = None) -> Optional[dict[str, Any]]:
        ...


class _CodeStoreLike(Protocol):
    """code_store 必须有的方法。"""
    def search_by_text(self, query_text: str, top_k: int = 10) -> list[tuple[str, float]]:
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
        *,
        business_store: _BusinessStoreLike,
        code_store: Optional[_CodeStoreLike],
        project_id: str,
    ):
        """构造：注入 BI / code_store / project_id。

        :param business_store: 主要数据源（BusinessInterpretation adapter）
        :param code_store: 兜底数据源（CodeEntity vector store）；None 时跳过兜底
        :param project_id: 当前请求绑定的工程 ID（写入 fallback candidates 用）
        """
        self._business_store = business_store
        self._code_store = code_store
        self._project_id = project_id

    # ─── BusinessStoreProto.search_method_hits_by_text ────────────────────

    def search_method_hits_by_text(
        self,
        *,
        text: str,
        project_id: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """先 BI，BI 空/失败 → CodeEntity 兜底。

        永不抛：fail-soft 设计，让 caller 拿 [] 走原有"未找到"路径。
        """
        # 1. 调 BI；catch 所有异常分流
        try:
            bi_hits = self._business_store.search_method_hits_by_text(
                text=text, project_id=project_id, limit=limit
            )
        except Exception as exc:
            # tenant_not_found 是预期分流（INFO 级别）；其它异常 WARNING
            if _is_tenant_missing(exc):
                _LOG.info(
                    "BI tenant 不存在 (project_id=%s)，走 CodeEntity 兜底",
                    project_id,
                )
            else:
                _LOG.warning(
                    "BI search 失败，走 CodeEntity 兜底 (project_id=%s): %s: %s",
                    project_id, type(exc).__name__, exc,
                )
            return self._code_fallback(text=text, limit=limit)

        # 2. BI 有数据 → 直接返
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
        """
        try:
            # 尝试用 modern signature（带 project_id kwarg）
            return self._business_store.get_by_entity(
                entity_id, project_id=self._project_id, level=level
            )
        except TypeError:
            # 兼容旧 signature（无 project_id 参数）
            try:
                return self._business_store.get_by_entity(entity_id, level=level)
            except Exception as exc:
                self._log_get_by_entity_exc(entity_id, exc)
                return None
        except Exception as exc:
            self._log_get_by_entity_exc(entity_id, exc)
            return None

    def _log_get_by_entity_exc(self, entity_id: str, exc: Exception) -> None:
        """get_by_entity 内部异常的日志分流。"""
        if _is_tenant_missing(exc):
            _LOG.debug(
                "get_by_entity tenant 不存在 (entity_id=%s, project_id=%s)",
                entity_id, self._project_id,
            )
        else:
            _LOG.warning(
                "get_by_entity 失败 (entity_id=%s, project_id=%s): %s: %s",
                entity_id, self._project_id, type(exc).__name__, exc,
            )

    # ─── 私有：CodeEntity 兜底 ────────────────────────────────────────────

    def _code_fallback(self, *, text: str, limit: int) -> list[dict[str, Any]]:
        """从 CodeEntity 向量库取候选，归一化成 BusinessStoreProto 期望的 dict。

        永不抛：code_store 异常 → 返 []。
        """
        # 1. code_store 未注入 → 跳过
        if self._code_store is None:
            _LOG.debug("code_store 未注入，跳过 CodeEntity 兜底")
            return []

        # 2. 调 code_store
        try:
            # WeaviateVectorStore.search_by_text(query_text, top_k) -> [(eid, score), ...]
            hits = self._code_store.search_by_text(text, top_k=limit)
        except Exception as exc:
            _LOG.warning(
                "CodeEntity fallback 失败 (project_id=%s): %s: %s",
                self._project_id, type(exc).__name__, exc,
            )
            return []

        # 3. 归一化（列表推导式）
        # level="code_entity" 让 LLM 判断分支知道这是代码层数据，业务解读缺失
        return [
            {
                "entity_id": eid,
                "summary_text": "",       # 实事求是：CodeEntity 没业务解读
                "level": "code_entity",   # 标记兜底来源
                # 不带 name / location —— 节省 token，QARetriever 后续扩展时补
                # 不带 neighbors —— 与 BI 路径同型
            }
            for (eid, _score) in hits
        ]
```

- [ ] **Step 5: 跑测试 expect 9 PASS**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_knowledge/test_composite_knowledge_store.py -v 2>&1 | tail -15
```

Expected: **9 passed**（2 个 _is_tenant_missing helper 测试 + 7 个 search_method_hits_by_text 测试）。

- [ ] **Step 6: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/knowledge/composite_knowledge_store.py \
        tests/test_knowledge/__init__.py \
        tests/test_knowledge/test_composite_knowledge_store.py
git commit -m "$(cat <<'EOF'
feat(knowledge): CompositeKnowledgeStore — BI + CodeEntity 双源兜底适配器

新增类实现 BusinessStoreProto，包装 (business_store, code_store, project_id)：
- search_method_hits_by_text: BI 有数据 → 用 BI；BI 空/抛 tenant_not_found/抛其它异常 → 走 CodeEntity 兜底
- CodeEntity 兜底：code_store.search_by_text(text, top_k=limit) → 归一化为
  {entity_id, summary_text="", level="code_entity"}，无 neighbors（QARetriever 后置扩展）
- get_by_entity: 完整实现（含 TypeError fallback 兼容 modern adapter + 异常分流）；
  Task 2 补 3 单测覆盖。

设计原则：fail-soft，永不抛；tenant_not_found 走 INFO 日志，其它异常 WARNING。

9 个单测：2 helper（_is_tenant_missing）+ 7 search（BI 命中/空/异常 + code 失败/None）。

设计：[[ReAct-代码层兜底-设计]] §2 §5

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: get_by_entity 完善 + 3 单测

**Files:**
- Modify: `tests/test_knowledge/test_composite_knowledge_store.py`（追加 3 测试）
- Modify: `src/knowledge/composite_knowledge_store.py`（get_by_entity 已在 Task 1 完成 — 本 task 只补测试覆盖）

> 注：Task 1 已实现 `get_by_entity` 方法（含 TypeError fallback + 异常分流）。Task 2 只补 3 个测试验证行为。

- [ ] **Step 1: 在 test_composite_knowledge_store.py 末尾追加 3 个测试**

```python
# ─── get_by_entity 3 个测试 ────────────────────────────────────────────────


def test_get_by_entity_delegates_to_bi():
    """get_by_entity 直接代理 BI 的结果。"""
    expected_record = {
        "entity_id": "method//foo",
        "summary_text": "处理用户登录",
        "level": "method",
    }
    composite, bi, code = _make_composite()
    # `return_value` 在 mock 没 side_effect 时直接返
    bi.get_by_entity.return_value = expected_record

    result = composite.get_by_entity("method//foo", level="method")

    assert result == expected_record
    # 验证带 project_id 调用（adapter 风格）
    bi.get_by_entity.assert_called_once_with(
        "method//foo", project_id="mall-swarm", level="method"
    )


def test_get_by_entity_tenant_not_found_returns_none(caplog):
    """BI.get_by_entity 抛 tenant_not_found → catch + 返 None，DEBUG 日志。"""
    composite, bi, code = _make_composite()
    bi.get_by_entity.side_effect = RuntimeError("tenant not found: mall-swarm")

    import logging
    with caplog.at_level(logging.DEBUG, logger="src.knowledge.composite_knowledge_store"):
        result = composite.get_by_entity("method//bar")

    assert result is None
    # tenant_not_found 走 DEBUG（高频，避免噪音）
    assert any("tenant 不存在" in rec.message for rec in caplog.records)


def test_get_by_entity_generic_error_warns_returns_none(caplog):
    """BI.get_by_entity 抛非 tenant_not_found 异常 → WARNING + 返 None。"""
    composite, bi, code = _make_composite()
    bi.get_by_entity.side_effect = ConnectionError("network down")

    import logging
    with caplog.at_level(logging.WARNING, logger="src.knowledge.composite_knowledge_store"):
        result = composite.get_by_entity("method//baz")

    assert result is None
    # generic error 走 WARNING（低频，要看见）
    assert any("get_by_entity 失败" in rec.message and "ConnectionError" in rec.message
               for rec in caplog.records)
```

- [ ] **Step 2: 跑测试 expect 12 PASS（9 + 3）**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_knowledge/test_composite_knowledge_store.py -v 2>&1 | tail -20
```

Expected: **12 passed**。

- [ ] **Step 3: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add tests/test_knowledge/test_composite_knowledge_store.py
git commit -m "$(cat <<'EOF'
test(knowledge): CompositeKnowledgeStore.get_by_entity 补 3 单测

覆盖 Task 1 已实现的 get_by_entity 方法：
- delegates_to_bi: 直接代理 BI，带 project_id 调用
- tenant_not_found_returns_none: tenant 异常走 DEBUG + 返 None
- generic_error_warns_returns_none: 其它异常走 WARNING + 返 None

共 12 单测全过。

设计：[[ReAct-代码层兜底-设计]] §6

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: DI 接线 — qa_router 注入 CompositeKnowledgeStore

**Files:**
- Modify: `src/service/qa_router.py:67-105`（build_retriever_for_project）
- Modify: `src/service/qa_router.py:102-160`（build_tools_for_project）

- [ ] **Step 1: 改 `build_retriever_for_project`（在文件中找现有实现）**

打开 `src/service/qa_router.py`，找到这段：

```python
def build_retriever_for_project(project_id: str, request: Request):
    # ...
    biz_store = getattr(request.app.state, "weaviate_business_store", None)
    neo4j_backend = getattr(request.app.state, "neo4j_backend", None)

    if biz_store is None or neo4j_backend is None:
        raise RuntimeError(...)

    from src.service.qa_engine.adapters import Neo4jGraphAdapter, WeaviateBusinessAdapter
    from src.service.qa_engine.retriever import QARetriever

    biz_adapter = WeaviateBusinessAdapter(biz_store)
    graph_adapter = Neo4jGraphAdapter(neo4j_backend, project_id=project_id)

    return QARetriever(business_store=biz_adapter, graph=graph_adapter)
```

把 `return QARetriever(...)` 前面加 composite 包装：

```python
    biz_adapter = WeaviateBusinessAdapter(biz_store)
    graph_adapter = Neo4jGraphAdapter(neo4j_backend, project_id=project_id)

    # ReAct 代码层兜底（Obsidian [[ReAct-代码层兜底-设计]]）：
    # mall-swarm 类工程无 BusinessInterpretation 数据时，用 CodeEntity 向量库兜底。
    # code_store 由 _try_connect_backends 在 startup 时连接到 app.state.weaviate_code_store；
    # 未连成功（None）时 composite 自动跳过 fallback，行为与原来一致。
    from src.knowledge.composite_knowledge_store import CompositeKnowledgeStore
    code_store = getattr(request.app.state, "weaviate_code_store", None)
    composite_store = CompositeKnowledgeStore(
        business_store=biz_adapter,
        code_store=code_store,
        project_id=project_id,
    )

    return QARetriever(business_store=composite_store, graph=graph_adapter)
```

- [ ] **Step 2: 改 `build_tools_for_project`**

在同一文件找到 `build_tools_for_project` 函数，找到这两行：

```python
    biz_adapter = WeaviateBusinessAdapter(biz_store)
    graph_adapter = Neo4jGraphAdapter(neo4j_backend, project_id=project_id)
```

在它们之后、`return build_default_registry(...)` 之前插入 composite 构造，并把 build_default_registry 的 `business_store=biz_adapter` 改成 `business_store=composite_store`：

```python
    biz_adapter = WeaviateBusinessAdapter(biz_store)
    graph_adapter = Neo4jGraphAdapter(neo4j_backend, project_id=project_id)

    # ke_search 工具也用 composite，BI 空时兜底走 CodeEntity（设计 §2）
    from src.knowledge.composite_knowledge_store import CompositeKnowledgeStore
    composite_store = CompositeKnowledgeStore(
        business_store=biz_adapter,
        code_store=code_store,
        project_id=project_id,
    )

    return build_default_registry(
        graph=graph_adapter,
        business_store=composite_store,   # ← 原 biz_adapter 改 composite_store
        project_id=project_id,
        code_store=code_store,
        method_interp_store=method_interp_store,
        repo_local_path=repo_local_path,
    )
```

- [ ] **Step 3: 跑回归测试**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth tests/test_knowledge -q 2>&1 | tail -8
```

Expected: 全 PASS（不应有任何回归 — composite 在 BI 有数据时透传 == 原 biz_adapter 行为）。

- [ ] **Step 4: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/qa_router.py
git commit -m "$(cat <<'EOF'
feat(qa-router): build_retriever / build_tools 注入 CompositeKnowledgeStore

把 biz_adapter 用 CompositeKnowledgeStore 再包一层，让 QARetriever 与 ke_search
在 BusinessInterpretation 空/失败时自动兜底用 CodeEntity 向量库。

行为兼容：
- BI 有数据（petclinic）→ composite 透传 → 零变更
- BI 空（mall-swarm）→ composite 走 CodeEntity 兜底 → candidates 含 level="code_entity"
- code_store=None（未连）→ composite 仍返 []（fail-soft）

设计：[[ReAct-代码层兜底-设计]] §2

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: AGENT_SYSTEM_PROMPT 改造 + 2 prompt 校验测试

**Files:**
- Modify: `tests/test_auth/test_qa_react_synthesizer.py`（追加 2 测试）
- Modify: `src/service/qa_engine/prompts.py:137-`（AGENT_SYSTEM_PROMPT）
- Modify: `src/service/qa_engine/react_synthesizer.py`（`_build_tool_usage_hint` 第 4 条加 1 行）

- [ ] **Step 1: 在 `test_qa_react_synthesizer.py` 末尾追加 2 个测试**

```python
# ─── AGENT_SYSTEM_PROMPT 改造校验 (ReAct 代码层兜底设计) ──────────────────


def test_agent_system_prompt_drops_giveup_clause():
    """AGENT_SYSTEM_PROMPT 不应再含'context 不足以回答时：直接说明未找到'"""
    from src.service.qa_engine.prompts import AGENT_SYSTEM_PROMPT
    # 旧 prompt 第 4 条原文（应被删除）
    forbidden = "context 不足以回答时：直接说明"
    assert forbidden not in AGENT_SYSTEM_PROMPT, (
        f"AGENT_SYSTEM_PROMPT 仍包含旧的放弃指令: {forbidden!r}；"
        f"应在 ReAct 代码层兜底改造中删除（设计 §4）"
    )


def test_agent_system_prompt_contains_exploration_flow():
    """AGENT_SYSTEM_PROMPT 必须包含'探索流程'引导段。"""
    from src.service.qa_engine.prompts import AGENT_SYSTEM_PROMPT
    # 关键标记短语（必须同时存在）
    required = [
        "探索流程",           # 段标题
        "ke_search 用问题里",  # 第一步：扩大候选
        "level=\"code_entity\"",  # 兜底标记的判断
        "不要直接放弃",        # 反指令
    ]
    missing = [p for p in required if p not in AGENT_SYSTEM_PROMPT]
    assert not missing, f"AGENT_SYSTEM_PROMPT 缺少探索流程关键短语: {missing}"
```

- [ ] **Step 2: 跑测试 expect FAIL**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_qa_react_synthesizer.py::test_agent_system_prompt_drops_giveup_clause tests/test_auth/test_qa_react_synthesizer.py::test_agent_system_prompt_contains_exploration_flow -v 2>&1 | tail -15
```

Expected: 2 个测试 FAIL（旧 prompt 仍含放弃指令；缺探索流程）。

- [ ] **Step 3: 改 `src/service/qa_engine/prompts.py` 的 AGENT_SYSTEM_PROMPT**

找到 AGENT_SYSTEM_PROMPT 定义（line ~137），具体定位第 4 条规则与【Mermaid 约定】之间。

**删除**这一行（line ~148）：
```
4. context 不足以回答时：直接说明"未找到相关业务逻辑，建议换个说法"，不要硬编。
```

**在原 4 条规则的位置后、【Mermaid 约定】之前插入新一段**（保持 4 条原数据流向）：

```markdown
【探索流程（重要：判断 context 是否充足）】

判断 context 充足的标准：
  - candidates 数量 ≥ 3 且至少有一个 level 不是 "code_entity" → 充足
  - candidates 全部是 level="code_entity" → 仅代码层数据，**业务解读缺失**
  - candidates 为空 → context 严重不足

context 不足时**不要直接放弃**，先用工具探索：

1. **第一步：扩大候选**
   - 不知道叫什么 → ke_search 用问题里的【关键词 / 类名 / 方法名 / 业务词】查
   - 关键词模糊 → 用 ke_glob 找文件名 / ke_grep 找代码常量

2. **第二步：理解候选**
   - 拿到 entity_id → ke_callees / ke_callers 看依赖
   - 想看代码 → ke_read_entity 看 attrs + code_snippet
   - 想看技术解读 → ke_method_interp（无解读也 ok，至少有 signature）

3. **第三步：判定是否真的没有**
   - 探索 2-3 轮后仍无有用结果 → 输出"我尝试了 ke_search('xxx') / ke_callees(yyy) 等工具，未能找到符合的 entity。建议补充：1) 完整类全限定名 2) 业务关键词 3) entity_id"
   - **不要无尝试就投降**

特殊情况：candidates 全是 level="code_entity"（业务解读缺失）：
  - 说明此工程只跑了代码索引，没跑业务解读
  - 你能基于代码本身解读：方法签名、调用关系、SQL preview（MyBatis）等
  - **不要**因 summary_text 为空就说"未找到"——代码层数据已经足够给出有意义的回答
```

- [ ] **Step 4: 改 `src/service/qa_engine/react_synthesizer.py` 的 `_build_tool_usage_hint`**

找到 `_build_tool_usage_hint` 方法体里第 4 条 "能给最终答案就别再调工具"，在它末尾追加一行：

```python
# 旧第 4 条最后一行（合并下面新行）:
# 4. **能给最终答案就别再调工具**。tool_call 仅用于"我看了 candidates 还差关键信息"的场景；
#    如果 candidates 已经足够回答，直接输出 6 段式 JSON。

# 改成：
# 4. **能给最终答案就别再调工具**。tool_call 仅用于"我看了 candidates 还差关键信息"的场景；
#    如果 candidates 已经足够回答，直接输出 6 段式 JSON。
#    **但**如果 candidates 全是 level="code_entity"（业务解读缺失），即使候选齐全也至少调 1 次
#    ke_callees / ke_read_entity 补齐代码细节。
```

具体改动定位 `_build_tool_usage_hint` 函数体 f-string 内的第 4 条 — 在 "直接输出 6 段式 JSON。" 结束的位置追加一句（在同一字符串里）。

- [ ] **Step 5: 跑 prompt 测试 expect PASS**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_qa_react_synthesizer.py::test_agent_system_prompt_drops_giveup_clause tests/test_auth/test_qa_react_synthesizer.py::test_agent_system_prompt_contains_exploration_flow -v 2>&1 | tail -10
```

Expected: 2 PASS。

- [ ] **Step 6: 全套回归测试**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth tests/test_knowledge tests/test_structure tests/test_semantic -q 2>&1 | tail -5
```

Expected: 全 PASS（700+ + 12 composite + 2 prompt = ~745+ 全过）。

- [ ] **Step 7: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/qa_engine/prompts.py \
        src/service/qa_engine/react_synthesizer.py \
        tests/test_auth/test_qa_react_synthesizer.py
git commit -m "$(cat <<'EOF'
feat(qa-engine): AGENT_SYSTEM_PROMPT 删放弃指令 + 加探索流程段

针对 mall-swarm 类无 BI 工程：原 prompt 第 4 条"context 不足直接说未找到"
让 LLM 看到空 context 直接投降不调工具。改造：

- prompts.py: AGENT_SYSTEM_PROMPT 删旧第 4 条；新增【探索流程】段：
    · context 充足判定（candidates 数量 + level）
    · 三步探索（扩大候选 → 理解候选 → 判定是否真的没有）
    · 特殊情况 level="code_entity"：基于代码本身解读，不要因 summary_text 空就放弃
- react_synthesizer.py: _build_tool_usage_hint 第 4 条加 1 行
    （candidates 全 code_entity 时即使齐全也至少调 1 次 ke_callees/read_entity）

2 prompt 校验测试 + 全套回归（~745 pass）。

设计：[[ReAct-代码层兜底-设计]] §4

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: E2E 手测 mall-swarm 验证 ReAct 真起飞

**Files:** （无代码改动）

- [ ] **Step 1: 确认 MySQL tunnel + Weaviate / Neo4j 连通**

```bash
lsof -nP -iTCP:3307 -sTCP:LISTEN 2>/dev/null | head -2
# 若无输出 → 手动跑 bash /Users/java/knowledge-engineering-auth/scripts/start_mysql_tunnel.sh
```

- [ ] **Step 2: 重启 uvicorn（让 prompt + composite store 改动 reload）**

```bash
pkill -f "uvicorn src.service.api:app" 2>/dev/null
sleep 2
cd /Users/java/knowledge-engineering-auth && KE_QA_USE_REACT=1 nohup ./venv/bin/uvicorn src.service.api:app --host 127.0.0.1 --port 8000 --reload > /tmp/uvicorn-react.log 2>&1 &
sleep 6
# 启动 log 应见 mode=ReAct
grep "qa_engine ready" /tmp/uvicorn-react.log | tail -2
```

Expected: `qa_engine ready (model=qwen-turbo, mode=ReAct, max_iter=12)`。

- [ ] **Step 3: 登录 alice 拿 token**

```bash
TOKEN=$(curl -sS -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"alice","password":"test12345"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
echo "$TOKEN" > /tmp/alice.token
echo "TOKEN len=${#TOKEN}"
```

Expected: TOKEN len > 100。

- [ ] **Step 4: 跑 mall-swarm 真实问答**

```bash
curl -sS -N -X POST http://localhost:8000/projects/mall-swarm/qa/explain \
  -H "Authorization: Bearer $(cat /tmp/alice.token)" \
  -H "Content-Type: application/json" \
  -d '{"question":"谁调用了 UmsRoleDao.getMenuList？这个方法的依赖关系如何？真实 SQL 是什么？"}' \
  --max-time 180 > /tmp/qa-mall-swarm-react.sse 2>&1
```

- [ ] **Step 5: 验证 SSE 事件**

```bash
echo "---event counts---"
grep "^event:" /tmp/qa-mall-swarm-react.sse | sort | uniq -c

echo "---meta (route + skill)---"
grep -A1 "^event: meta" /tmp/qa-mall-swarm-react.sse | head -2

echo "---tool_call events (验收关键)---"
grep -A1 "^event: tool_call" /tmp/qa-mall-swarm-react.sse | head -20

echo "---cited_entities---"
grep "cited_entities" /tmp/qa-mall-swarm-react.sse | tail -1
```

**验收标准（必须全满足）**：
- ✅ event counts 含 `tool_call` ≥ 2（LLM 主动调工具）
- ✅ meta 的 `route_source` 是 `keyword`，`skill_id` 是 `dependency`
- ✅ tool_call 至少一个是 `ke_search` 或 `ke_callees` / `ke_callers`
- ✅ cited_entities 含 ≥ 1 个真实 mall-swarm entity_id（形如 `method//xxx`）
- ✅ 答案非旧固定模板"未找到相关业务逻辑，建议换个说法"

- [ ] **Step 6: 抽样查看一段答案**

```bash
echo "---reconstructed answer---"
grep "^event: token" -A1 /tmp/qa-mall-swarm-react.sse | grep "delta" | python3 -c "
import sys, json
out = []
for line in sys.stdin:
    line = line.strip()
    if not line.startswith('data:'):
        continue
    try:
        d = json.loads(line[5:].strip())
        if 'delta' in d:
            out.append(d['delta'])
    except: pass
print(''.join(out))" 2>&1 | head -40
```

Expected：自然 markdown 答案，含 `[entity_id|文本]` 引用标记、SQL preview、调用关系描述。

- [ ] **Step 7: 若 Step 5 验收失败 → 看 uvicorn log 诊断**

```bash
tail -50 /tmp/uvicorn-react.log
```

可能原因：
- BI tenant 不存在的日志缺：composite 没被注入 → 回 Task 3
- LLM 没调工具：探索流程指令未生效 → 回 Task 4 检查 prompt diff
- code_store 未连：检查 startup 时 `app.state.weaviate_code_store is None`，需追到 `_try_connect_backends`

- [ ] **Step 8: 通过后 commit（如有 fixup）**

如果 Step 4-6 暴露了什么 corner case 需要 fixup，单独 fixup commit。如果一切正常，无需 commit。

---

## Task 6: Obsidian §11 实施完成标记

**Files:**
- Modify: `/Users/java/obsidian/01 Engineering/knowledge-engineering/ReAct-代码层兜底-设计.md`（追加 §11）

- [ ] **Step 1: 收集 commit SHA**

```bash
cd /Users/java/knowledge-engineering-auth && git log --oneline release-0513..HEAD | head -10
```

记录 6 个 commit 的 SHA（Task 1-4 各一，Task 5 可能 fixup）。

- [ ] **Step 2: 在 spec 文末追加 §11**

打开 `/Users/java/obsidian/01 Engineering/knowledge-engineering/ReAct-代码层兜底-设计.md`，在 `*父设计：[[代码解读Agent引擎-设计]] ...*` 之前追加：

```markdown
---

## §11 实施完成（2026-05-28）

4 个核心 task 完成，全套回归 ~745 pass，mall-swarm 端到端验证通过。

### Commits 列表（自 spec 起，按时间顺序）

| Task | Commit | 内容 |
|---|---|---|
| 1 | `<sha1>` | CompositeKnowledgeStore 主体 + search_method_hits_by_text + 9 单测 |
| 2 | `<sha2>` | get_by_entity 补 3 单测 |
| 3 | `<sha3>` | qa_router build_retriever / build_tools 注入 Composite |
| 4 | `<sha4>` | AGENT_SYSTEM_PROMPT 删放弃 + 加探索流程 + 2 prompt 测试 |
| 5 | （手测无 commit） | mall-swarm E2E 验证 |

### 实测数据（mall-swarm，2026-05-28）

| 指标 | 数据 | 备注 |
|---|---|---|
| BI tenant 是否存在 | ❌ 不存在 | mall-swarm 跑 P0 时用 --without-interpretation |
| CodeEntity tenant 数据 | 7789 ✅ | DashScope 1024 维 |
| Composite fallback 触发 | ✅ | log 含 "BI tenant 不存在 (project_id=mall-swarm)，走 CodeEntity 兜底" |
| LLM 主动调工具次数 | TBD（≥ 2 验收） | 替换为实测 |
| 答案 cited_entities | TBD（≥ 1 验收） | 替换为实测 |
| 单测覆盖 | 12 composite + 2 prompt | 全 pass |

### 已知 follow-up（spec §8 列出）

1. WeaviateVectorStore.exists / exists_many API 实装
2. --force-full 加 Weaviate tenant 自动清空
3. SkillRouter 上 LLM fallback
4. 跑 mall-swarm --with-interpretation 重建业务解读库
5. 指标采集（兜底触发率 / CodeEntity vs BI 命中率分布）
6. subagent (ke_investigate) 模式
```

填实际 commit SHA + Step 5 实测数据。

- [ ] **Step 3: 文档落盘即可（vault 非 git）**

无需 commit Obsidian 文档（用户 CLAUDE.md：vault 非 git，文档直接保存）。

---

## Self-Review

### 1. Spec 覆盖

| Spec 段 | Task |
|---|---|
| §0 背景与问题 | Plan Goal + Architecture |
| §1 决策表（Q1-Q5 + 实现方案 A） | 各 Task 实现细节 |
| §2 架构 + 文件清单 | File Structure 表 + Task 1-4 各文件清单 |
| §3 数据流（场景 A/B/C） | Task 1（构造 mock 覆盖三种）+ Task 5（mall-swarm 真验场景 B） |
| §4 AGENT_SYSTEM_PROMPT 改造 | Task 4 |
| §5 错误处理（异常路径表） | Task 1 测试 4 / 6（异常 + WARNING 日志）+ Task 2 测试（tenant_not_found + generic） |
| §6 测试策略 10 单测 | Task 1 (7) + Task 2 (3) + Task 4 (2 prompt) = 12 + 2 |
| §7 决策日志 | Plan Architecture 文本里有引用 |
| §8 follow-up | Task 6 §11 列入 |
| §9 涉及文件清单 | File Structure 表 |

**全覆盖** ✅。

### 2. Placeholder scan

每个 step 都有真实 Python / shell code 或者 commit message HEREDOC；TBD 只在 Task 6 §11 实测数据（这是预期 — 要 Task 5 实测完才能填）。无其它 TBD。

### 3. Type / signature 一致

- `CompositeKnowledgeStore(business_store, code_store, project_id)` — Task 1 定义；Task 3 DI 使用，参数名一致
- `search_method_hits_by_text(*, text, project_id, limit=5) -> list[dict]` — 与 BusinessStoreProto 一致
- `_code_fallback(*, text, limit) -> list[dict]` — 内部方法，Task 1 定义即用
- `_is_tenant_missing(exc) -> bool` — Task 1 定义，模块级 helper
- `get_by_entity(entity_id, level=None) -> dict | None` — Task 1 定义（已带 TypeError fallback 兼容 modern adapter），Task 2 补测试
- `code_store.search_by_text(query_text, top_k)` — 现有 WeaviateVectorStore 方法，签名一致

一致 ✅。

---

## Execution Handoff

Plan 写完，落盘到 `docs/superpowers/plans/2026-05-28-react-code-fallback.md`。两种执行方式：

**1. Subagent-Driven（推荐）**：每个 task dispatch 一个 fresh subagent，task 间 review

**2. Inline Execution（这个 session 直接跑）**：用 executing-plans batch 跑，checkpoint 中间审

哪种？
