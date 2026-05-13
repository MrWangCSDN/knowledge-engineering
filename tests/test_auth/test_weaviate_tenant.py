"""测试 WeaviateBusinessAdapter 的 Native Multi-Tenancy 支持（Task 20）。

验证 v2.0 核心改动：search_method_hits_by_text 和 get_by_entity 都必须通过
with_tenant(project_id) 把查询绑定到对应的 Weaviate tenant 分区。

不依赖真实 Weaviate 连接，全用 MagicMock 模拟。
"""
# pytest 是 Python 最流行的测试框架；fixture 用于共享测试前置资源
import pytest
# unittest.mock：Python 标准库的 mock 工具
# MagicMock：可以模拟任何对象（自动创建属性 / 方法 / 返回值）
# patch：临时替换模块中的某个名字（用完自动还原）
from unittest.mock import MagicMock, patch

# 被测对象：WeaviateBusinessAdapter
from src.service.qa_engine.adapters import WeaviateBusinessAdapter


# ─── 工具函数：构造一个配置好的 fake store ───────────────────────────────────

def _make_adapter_with_fake_store():
    """返回 (adapter, fake_store, fake_collection, fake_tenant_view) 四元组。

    fake_collection.with_tenant(x) 会返回 fake_tenant_view，
    测试里验证 with_tenant 是否被正确调用。
    """
    # MagicMock() 创建一个"万能假对象"，访问任何属性 / 方法都不会报错
    fake_store = MagicMock()
    fake_collection = MagicMock()
    fake_tenant_view = MagicMock()

    # 配置调用链：store._get_collection() → fake_collection
    fake_store._get_collection.return_value = fake_collection
    # 配置调用链：collection.with_tenant(任意值) → fake_tenant_view
    fake_tenant_view = MagicMock()
    fake_collection.with_tenant.return_value = fake_tenant_view

    # 维度属性（向量化时用到）
    fake_store._dim = 1024
    # collection 名（near_vector_property_hits 用到）
    fake_store._collection_name = "BusinessInterpretation"

    # 创建 adapter 并注入 fake_store
    adapter = WeaviateBusinessAdapter(fake_store)
    return adapter, fake_store, fake_collection, fake_tenant_view


# ─── search_method_hits_by_text：tenant 绑定 ────────────────────────────────

def test_search_calls_with_tenant_using_project_id():
    """search_method_hits_by_text 应该用 project_id 做 tenant 调 with_tenant。

    这是 Task 20 最核心的断言：每次语义检索都必须走 tenant-scoped 视图，
    确保多工程数据物理隔离。
    """
    adapter, fake_store, fake_collection, fake_tenant_view = _make_adapter_with_fake_store()

    # adapters.py 在函数体内做 lazy import：
    #   from src.semantic.embedding import get_embedding
    #   from src.knowledge.weaviate_near_vector import near_vector_property_hits
    # 所以 patch 的目标是这两个模块的实际路径，而不是 adapters 模块级名字
    # create=True：如果目标属性还不存在（模块未被 import 过），也强制创建
    with patch("src.semantic.embedding.get_embedding", return_value=[0.1] * 1024, create=True), \
         patch("src.knowledge.weaviate_near_vector.near_vector_property_hits", return_value=[], create=True):
        adapter.search_method_hits_by_text(text="查询宠物信息", project_id="petclinic", limit=5)

    # 核心断言：with_tenant 必须被调用一次，且参数是 project_id
    # assert_called_once_with 是 MagicMock 的断言方法，验证"被精确调用一次且参数正确"
    fake_collection.with_tenant.assert_called_once_with("petclinic")


def test_search_passes_tenant_view_to_near_vector():
    """near_vector_property_hits 收到的第一个参数应该是 tenant-scoped 视图，而不是原 collection。

    with_tenant 返回的 TenantView 才是真正限定了 tenant 分区的查询对象。
    """
    adapter, fake_store, fake_collection, fake_tenant_view = _make_adapter_with_fake_store()

    # 用列表来捕获 near_vector_property_hits 的实际入参
    captured_calls = []

    def fake_near_vector(coll, **kwargs):
        """替代 near_vector_property_hits 的假实现，记录 coll 参数。"""
        captured_calls.append(coll)
        return []

    with patch("src.semantic.embedding.get_embedding", return_value=[0.1] * 1024, create=True), \
         patch("src.knowledge.weaviate_near_vector.near_vector_property_hits", side_effect=fake_near_vector, create=True):
        adapter.search_method_hits_by_text(text="查询宠物信息", project_id="petclinic", limit=5)

    # 必须有一次调用
    assert len(captured_calls) == 1
    # 传入的 collection 对象必须是 with_tenant 返回的 tenant_view，不是原始 collection
    assert captured_calls[0] is fake_tenant_view


# ─── search_method_hits_by_text：边界保护 ───────────────────────────────────

def test_search_returns_empty_when_project_id_empty():
    """project_id 为空字符串时，直接返回 []，不调用 Weaviate。

    防止 with_tenant("") 造成跨租户误查或 Weaviate 报错。
    """
    fake_store = MagicMock()
    adapter = WeaviateBusinessAdapter(fake_store)

    # 调用时 project_id="" → 应该直接 return []，不碰 store
    result = adapter.search_method_hits_by_text(text="任意问题", project_id="", limit=5)

    assert result == []
    # 保证没有调用 _get_collection（即完全短路，没有进入检索流程）
    fake_store._get_collection.assert_not_called()


def test_search_returns_empty_when_text_empty():
    """text 为空字符串时，直接返回 []，不调用 Weaviate。"""
    fake_store = MagicMock()
    adapter = WeaviateBusinessAdapter(fake_store)

    result = adapter.search_method_hits_by_text(text="", project_id="petclinic", limit=5)

    assert result == []
    fake_store._get_collection.assert_not_called()


def test_search_returns_empty_when_text_whitespace_only():
    """text 只有空白字符时也视为空，直接返回 []。"""
    fake_store = MagicMock()
    adapter = WeaviateBusinessAdapter(fake_store)

    result = adapter.search_method_hits_by_text(text="   \t\n", project_id="petclinic", limit=5)

    assert result == []
    fake_store._get_collection.assert_not_called()


# ─── search_method_hits_by_text：返回值映射 ─────────────────────────────────

def test_search_maps_rows_to_expected_keys():
    """near_vector 返回 (props, score) 列表时，adapter 应正确映射成 dict。

    验证每条结果都含 entity_id / level / summary_text / score 等字段。
    """
    adapter, fake_store, fake_collection, fake_tenant_view = _make_adapter_with_fake_store()

    # 构造模拟的 near_vector 返回值：list of (props_dict, score_float)
    fake_rows = [
        (
            {
                "entity_id": "method://com.example.PetController.findAll",
                "entity_type": "method",
                "level": "api",
                "summary_text": "查询所有宠物列表",
                "business_domain": "宠物管理",
                "business_capabilities": "列表查询",
                "language": "Java",
            },
            0.92,
        )
    ]

    with patch("src.semantic.embedding.get_embedding", return_value=[0.1] * 1024, create=True), \
         patch("src.knowledge.weaviate_near_vector.near_vector_property_hits", return_value=fake_rows, create=True):
        result = adapter.search_method_hits_by_text(text="查询宠物", project_id="petclinic", limit=5)

    # 应该返回 1 条，且结构正确
    assert len(result) == 1
    row = result[0]
    assert row["entity_id"] == "method://com.example.PetController.findAll"
    assert row["level"] == "api"
    assert row["summary_text"] == "查询所有宠物列表"
    # score 应该被包含（float 类型）
    assert abs(row["score"] - 0.92) < 1e-6


# ─── get_by_entity：tenant 绑定 ─────────────────────────────────────────────

def test_get_by_entity_calls_with_tenant_method():
    """get_by_entity 应优先调用 get_by_entity_with_tenant（带 tenant 参数）。

    Task 23 会在主仓添加此方法；Task 20 的 adapter 已经调用它（如果存在）。
    """
    fake_store = MagicMock()
    # MagicMock 默认会自动创建任何被访问的属性 / 方法，包括 get_by_entity_with_tenant
    fake_store.get_by_entity_with_tenant.return_value = {
        "entity_id": "method://x", "summary_text": "test"
    }

    adapter = WeaviateBusinessAdapter(fake_store)
    result = adapter.get_by_entity("method://x", project_id="petclinic")

    # 应该调用了带 tenant 的方法
    fake_store.get_by_entity_with_tenant.assert_called_once_with(
        "method://x", tenant="petclinic", level=None
    )
    assert result is not None


def test_get_by_entity_fallback_when_with_tenant_not_implemented():
    """当主仓没有 get_by_entity_with_tenant 时（AttributeError），fallback 到无 tenant 版本。

    这是 Task 20 的兼容性保证：Task 23 完成前，现有测试不会因主仓缺方法而崩溃。
    """
    fake_store = MagicMock()
    # 让 get_by_entity_with_tenant 抛 AttributeError（模拟"方法不存在"）
    # spec=object 让 MagicMock 严格限制只有 object 的方法，访问其他属性就报 AttributeError
    # 这里用更直接的方式：直接把属性设成 property 抛异常
    del fake_store.get_by_entity_with_tenant  # 删掉 MagicMock 自动创建的属性

    # 在 MagicMock 上删除属性后再次访问会重建；用 side_effect 更可靠
    fake_store.get_by_entity_with_tenant = MagicMock(side_effect=AttributeError("no such method"))
    fake_store.get_by_entity.return_value = {"entity_id": "method://x", "summary_text": "fallback"}

    adapter = WeaviateBusinessAdapter(fake_store)
    result = adapter.get_by_entity("method://x", project_id="petclinic")

    # fallback 路径：应该调用了无 tenant 的版本
    fake_store.get_by_entity.assert_called_once_with("method://x", level=None)
    assert result == {"entity_id": "method://x", "summary_text": "fallback"}


def test_get_by_entity_returns_none_on_unexpected_error():
    """get_by_entity 遇到非 AttributeError 的异常时，返回 None（不上抛）。"""
    fake_store = MagicMock()
    # 模拟网络超时等意外异常
    fake_store.get_by_entity_with_tenant.side_effect = TimeoutError("connection timeout")

    adapter = WeaviateBusinessAdapter(fake_store)
    result = adapter.get_by_entity("method://x", project_id="petclinic")

    # 不抛异常，返回 None
    assert result is None
