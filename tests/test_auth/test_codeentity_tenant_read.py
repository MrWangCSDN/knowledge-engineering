"""WeaviateVectorStore.get_by_entity_id 的 multi-tenant 读取（P1 source-first grounding 发现的 bug）。

线上 CodeEntity collection 启用 multi-tenancy（写入带 tenant=project_id），但 get_by_entity_id
原先查询未 with_tenant → Weaviate 抛 "multi-tenancy enabled, but request was without tenant"
→ 被 except 吞掉返 None → ke_read_entity 与 P1 预读全部静默失效。本测试锁定 with_tenant 绑定。
"""
from unittest.mock import MagicMock
# 被测：真实 WeaviateVectorStore（以未绑定函数 + fake self 方式调用，不连真 Weaviate）
from src.knowledge.vector_store_weaviate import WeaviateVectorStore


def _fake_obj(props):
    """构造一个带 .properties 的假 Weaviate 对象（fetch_objects().objects 的元素）。"""
    o = MagicMock()
    o.properties = props
    return o


def test_get_by_entity_id_binds_tenant_and_returns_snippet():
    """传 tenant 时：必须 with_tenant(tenant) 后再查，并返回 code_snippet。"""
    fake_self = MagicMock()              # 充当 WeaviateVectorStore 实例（只用到 _get_collection）
    fake_coll = MagicMock()              # _get_collection() 的返回
    tenant_view = MagicMock()            # with_tenant(t) 的返回（真正限定分区的视图）
    fake_self._get_collection.return_value = fake_coll
    fake_coll.with_tenant.return_value = tenant_view
    # 查询命中一条，properties 含 code_snippet
    tenant_view.query.fetch_objects.return_value = MagicMock(objects=[
        _fake_obj({"entity_id": "A::m#()", "name": "m", "entity_type": "method", "code_snippet": "CODE"})
    ])
    # 以未绑定函数调用，把 fake_self 当 self 注入
    out = WeaviateVectorStore.get_by_entity_id(fake_self, "A::m#()", tenant="mall-swarm")
    # 核心断言：用 tenant 调了 with_tenant
    fake_coll.with_tenant.assert_called_once_with("mall-swarm")
    assert out["code_snippet"] == "CODE"


def test_get_by_entity_id_without_tenant_skips_with_tenant():
    """不传 tenant（ke_read_entity 旧调用）：不调 with_tenant，保持向后兼容。"""
    fake_self = MagicMock()
    fake_coll = MagicMock()
    fake_self._get_collection.return_value = fake_coll
    fake_coll.query.fetch_objects.return_value = MagicMock(objects=[
        _fake_obj({"entity_id": "A::m#()", "code_snippet": "C"})
    ])
    out = WeaviateVectorStore.get_by_entity_id(fake_self, "A::m#()")
    fake_coll.with_tenant.assert_not_called()   # 无 tenant → 不绑定
    assert out["code_snippet"] == "C"
