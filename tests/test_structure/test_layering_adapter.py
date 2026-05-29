# tests/test_structure/test_layering_adapter.py
"""RuleBasedAdapter.classify 单测：验证各 match 信号 + 优先级 + fallback。"""
from src.config.models import LayeringConfig, LayerMatch, LayerSpec
from src.models.structure import EntityType, StructureEntity
from src.structure.layering.adapter import RuleBasedAdapter


def _cfg() -> LayeringConfig:
    """构造三层配置：entry 在前优先级最高，dao 用 name_suffix+language。"""
    return LayeringConfig(
        enabled=True, adapter="ssm", fallback_layer="unknown",
        layers=[
            LayerSpec(id="entry", name="入口层", match=LayerMatch(
                annotation=["Controller"], name_suffix=["Controller"], has_attr=["path"])),
            LayerSpec(id="business", name="业务层", match=LayerMatch(
                annotation=["Service"], name_suffix=["ServiceImpl", "Service"],
                package_contains=[".service"])),
            LayerSpec(id="dao", name="数据访问层", match=LayerMatch(
                language=["xml"], name_suffix=["Mapper", "Dao"])),
        ],
    )


def _cls(name, *, location="", language="java", attrs=None) -> StructureEntity:
    """构造一个 class 实体（最小字段）。"""
    return StructureEntity(id=f"class//{name}", type=EntityType.CLASS, name=name,
                           location=location, language=language, attributes=attrs or {})


def test_name_suffix_controller_is_entry():
    a = RuleBasedAdapter(_cfg())
    assert a.classify(_cls("PmsBrandController")) == "entry"


def test_annotation_service_is_business():
    a = RuleBasedAdapter(_cfg())
    # 名字不含 Service 后缀，但 annotations 含 Service → 命中 business
    e = _cls("OrderManager", attrs={"annotations": ["Service"]})
    assert a.classify(e) == "business"


def test_package_contains_service_is_business():
    a = RuleBasedAdapter(_cfg())
    e = _cls("Foo", location="mall/src/main/java/com/macro/mall/service/Foo.java:1")
    assert a.classify(e) == "business"


def test_language_xml_is_dao():
    a = RuleBasedAdapter(_cfg())
    e = _cls("getMenuList", language="xml")
    assert a.classify(e) == "dao"


def test_priority_first_layer_wins():
    """同时像 entry 和 business 时，layers 中靠前的 entry 优先。"""
    a = RuleBasedAdapter(_cfg())
    e = _cls("WeirdControllerService", attrs={"path": "/x"})  # 后缀 Service + 有 path
    assert a.classify(e) == "entry"


def test_fallback_when_no_match():
    a = RuleBasedAdapter(_cfg())
    assert a.classify(_cls("RandomUtil")) == "unknown"
