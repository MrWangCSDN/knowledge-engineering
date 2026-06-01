"""CompositeKnowledgeStore 单测 — mock BI / code store / graph，不连真后端。

设计：[[ReAct-代码层兜底-设计]] §6
"""
# unittest.mock：标准库，假对象工具
from unittest.mock import MagicMock

# logging：用于 caplog 测试日志输出（多处用，hoist 到顶部）
import logging

# 被测：first run 会 ImportError —— TDD RED 阶段
from src.knowledge.composite_knowledge_store import (
    CompositeKnowledgeStore,
    _is_tenant_missing,
)


# ─── fixture: 通用构造 ────────────────────────────────────────────────────


def _make_composite(
    bi_results=None,        # interpretation_store.search_method_hits_by_text 的返回值
    bi_exc=None,            # interpretation_store.search_method_hits_by_text 抛的异常（覆盖 bi_results）
    code_results=None,      # code_store.search_by_text 的返回值（[(eid, score), ...]）
    code_exc=None,
    has_code=True,          # 是否注入 code_store；False 模拟未连
):
    """造一个 CompositeKnowledgeStore + mock 子组件，返回 (composite, interp_mock, code_mock)。"""
    # interp 是 InterpretationStoreProto 兼容 mock（search_method_hits_by_text + get_by_entity）
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
        interpretation_store=bi,
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
    # code_store.search_by_text 被调一次，over-fetch=limit*4=20，带 tenant
    # over-fetch 策略：召回降噪+加权接线后 top_k=limit*4，给 rerank_and_filter 留出过滤空间
    code.search_by_text.assert_called_once_with("getMenuList 怎么用", top_k=20, tenant="mall-swarm")
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
    with caplog.at_level(logging.WARNING, logger="src.knowledge.composite_knowledge_store"):
        result = composite.search_method_hits_by_text(
            text="x", project_id="mall-swarm", limit=5
        )

    assert len(result) == 1
    # 验证日志：含 generic error 提示（Task 3 后 log message 改为"解读库"）
    assert any("ConnectionError" in rec.message for rec in caplog.records)


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

    with caplog.at_level(logging.DEBUG, logger="src.knowledge.composite_knowledge_store"):
        result = composite.get_by_entity("method//bar")

    assert result is None
    # tenant_not_found 走 DEBUG（高频，避免噪音）
    assert any("tenant 不存在" in rec.message for rec in caplog.records)


def test_get_by_entity_generic_error_warns_returns_none(caplog):
    """BI.get_by_entity 抛非 tenant_not_found 异常 → WARNING + 返 None。"""
    composite, bi, code = _make_composite()
    bi.get_by_entity.side_effect = ConnectionError("network down")

    with caplog.at_level(logging.WARNING, logger="src.knowledge.composite_knowledge_store"):
        result = composite.get_by_entity("method//baz")

    assert result is None
    # generic error 走 WARNING（低频，要看见）
    assert any("get_by_entity 失败" in rec.message and "ConnectionError" in rec.message
               for rec in caplog.records)


# ─── Task 1 code review carryover ──────────────────────────────────────────


def test_get_by_entity_legacy_signature_fallback():
    """legacy adapter 不接受 project_id kwarg → 走 TypeError fallback 用 2-arg 签名重试。

    覆盖 Task 1 code review I-1：让 TypeError 分支不再是 dead code。
    """
    expected_record = {"entity_id": "method//legacy", "level": "method"}
    composite, bi, code = _make_composite()

    # `side_effect` 用一个函数 / list 模拟"第一次抛 + 第二次返"行为
    # 这里用 list：[第 1 次结果, 第 2 次结果, ...]；元素是 Exception 实例就 raise，其它直接返
    bi.get_by_entity.side_effect = [
        TypeError("get_by_entity() got an unexpected keyword argument 'project_id'"),
        expected_record,
    ]

    result = composite.get_by_entity("method//legacy", level="method")

    # 第二次调用（fallback）的结果被返
    assert result == expected_record
    # 验证调用了两次：第一次带 project_id，第二次不带
    assert bi.get_by_entity.call_count == 2
    # 第一次是 modern signature
    first_call_kwargs = bi.get_by_entity.call_args_list[0].kwargs
    assert "project_id" in first_call_kwargs
    # 第二次是 legacy（无 project_id）
    second_call_kwargs = bi.get_by_entity.call_args_list[1].kwargs
    assert "project_id" not in second_call_kwargs


def test_code_fallback_dedupes_entity_ids():
    """code_store 返重复 entity_id → composite 归一化时去重保留首次。

    覆盖 Task 1 code review I-2：归一化层 dedup 防御。
    """
    code_hits = [
        ("method//a", 0.95),
        ("method//b", 0.85),
        ("method//a", 0.80),   # 重复 a
        ("method//c", 0.70),
        ("method//b", 0.65),   # 重复 b
    ]
    composite, bi, code = _make_composite(bi_results=[], code_results=code_hits)

    result = composite.search_method_hits_by_text(
        text="x", project_id="mall-swarm", limit=5
    )

    # 5 个 raw 但只 3 unique
    assert len(result) == 3
    # 顺序保留首次出现：a, b, c
    assert [r["entity_id"] for r in result] == ["method//a", "method//b", "method//c"]


def test_code_fallback_passes_tenant_to_code_store():
    """_code_fallback 调 code_store.search_by_text 时必须带 tenant=project_id。

    Task 5 E2E 修复：mall-swarm 7789 CodeEntity 都在 with_tenant('mall-swarm') 分区下，
    不传 tenant 会查到默认（空）分区。
    """
    composite, bi, code = _make_composite(
        bi_results=[],
        code_results=[("method//x", 0.9)],
    )

    composite.search_method_hits_by_text(
        text="UmsRoleDao", project_id="mall-swarm", limit=5
    )

    # 验证 search_by_text 被调时带 tenant kwarg，且 top_k 是 over-fetch 后的值（limit*4=20）
    # over-fetch 策略：DN-T2 接线后 top_k 变为 limit*4，rerank_and_filter 过滤后截回 limit
    code.search_by_text.assert_called_once_with(
        "UmsRoleDao", top_k=20, tenant="mall-swarm"
    )


def test_code_fallback_surfaces_score():
    """_code_fallback 归一化的 dict 必须带 score（= code_store 返回的相似度），供召回门控用。"""
    # 假 code_store：search_by_text 返回 [(entity_id, score)]
    class _FakeCodeStore:
        def search_by_text(self, text, top_k, tenant=None):
            return [("OmsOrderService::generateOrder#()", 0.66), ("X::y#()", 0.51)]

    # 假解读库：返回空 → 触发 CodeEntity 兜底
    class _EmptyInterp:
        def search_method_hits_by_text(self, *, text, project_id, limit=5):
            return []

    from src.knowledge.composite_knowledge_store import CompositeKnowledgeStore
    store = CompositeKnowledgeStore(
        interpretation_store=_EmptyInterp(), code_store=_FakeCodeStore(), project_id="mall-swarm",
    )
    hits = store.search_method_hits_by_text(text="下单", project_id="mall-swarm", limit=5)
    assert hits[0]["entity_id"] == "OmsOrderService::generateOrder#()"
    assert hits[0]["score"] == 0.66           # 透出真实分数
    assert hits[1]["score"] == 0.51


def test_code_fallback_filters_boilerplate_and_boosts_business():
    """_code_fallback over-fetch 后过滤样板、business 在前；score 保持原始。"""
    # 假 code_store：记录 last_top_k 供断言，返回混合了样板与业务的候选集
    class _FakeCodeStore:
        def __init__(self):
            self.last_top_k = None  # 记录最后一次调用的 top_k 参数

        def search_by_text(self, text, top_k, tenant=None):
            # 记录 top_k，测试用来验证 over-fetch 倍数是否正确
            self.last_top_k = top_k
            return [
                ("com.macro.mall.mapper.OmsOrderMapper::Base_Column_List#()", 0.70),  # drop：MyBatis 样板列名片段
                ("OmsOrderExample::andIdEqualTo#(Longv)", 0.69),                        # drop：XxxExample 条件构造器
                ("OmsOrder::getStatus#()", 0.66),                                       # demote：getter 访问器，降权
                ("OmsPortalOrderServiceImpl::generateOrder#(OrderParamp)", 0.62),       # boost：ServiceImpl 业务方法
                ("OmsOrderMapper::insert#(OmsOrderrow)", 0.55),                         # neutral：真实 Mapper 操作
            ]

    # 假解读库：返回空，触发 CodeEntity 兜底
    class _EmptyInterp:
        def search_method_hits_by_text(self, *, text, project_id, limit=5):
            return []  # 空列表 → 触发 _code_fallback

    from src.knowledge.composite_knowledge_store import CompositeKnowledgeStore
    fake = _FakeCodeStore()
    # 构造 CompositeKnowledgeStore，注入假 code_store
    store = CompositeKnowledgeStore(
        interpretation_store=_EmptyInterp(), code_store=fake, project_id="mall-swarm")
    # 调用时 limit=5，over-fetch 应取 limit*4=20
    hits = store.search_method_hits_by_text(text="下单", project_id="mall-swarm", limit=5)

    ids = [h["entity_id"] for h in hits]
    # 样板被过滤：Base_Column_List 和 XxxExample 系列不应出现在结果中
    assert all("Base_Column_List" not in e and "Example::" not in e for e in ids)
    # business 排第一：ServiceImpl::generateOrder 加权后应排到最前
    assert ids[0] == "OmsPortalOrderServiceImpl::generateOrder#(OrderParamp)"
    # over-fetch 验证：code_store 被调时 top_k 应为 limit*4=20
    assert fake.last_top_k == 20
    # score 保持原始：返回的 score 仍是 cosine 相似度，不是调整分
    top = next(h for h in hits if h["entity_id"].endswith("generateOrder#(OrderParamp)"))
    assert top["score"] == 0.62
