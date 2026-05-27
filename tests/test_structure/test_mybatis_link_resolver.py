"""mybatis_link_resolver 单元测试。

设计：[[MyBatis-XML-Extractor-设计]] §4 + §6.2
参考：CodeGraph callback-synthesizer.ts mybatisJavaXmlEdges()
"""
from src.models.structure import (
    EntityType,
    RelationType,
    StructureEntity,
    StructureFacts,
)
from src.structure.mybatis_link_resolver import (
    synthesize_mybatis_java_xml_relations,
)


def _java_method(class_fqn: str, method_name: str) -> StructureEntity:
    """构造一个 Java method entity（最小字段）。"""
    qualified = f"{class_fqn}::{method_name}"
    return StructureEntity(
        id=f"method//java-{class_fqn}-{method_name}",
        type=EntityType.METHOD,
        name=method_name,
        language="java",
        attributes={"qualified_name": qualified},
    )


def _xml_statement(namespace: str, id_: str) -> StructureEntity:
    """构造一个 XML statement entity。"""
    qualified = f"{namespace}::{id_}"
    return StructureEntity(
        id=f"method//xml-{namespace}-{id_}",
        type=EntityType.METHOD,
        name=id_,
        language="xml",
        attributes={"qualified_name": qualified, "namespace": namespace},
    )


def test_basic_match_synthesizes_edge():
    """单 Java method ↔ 单 XML statement，合成 1 个 CALLS 边。"""
    facts = StructureFacts(entities=[
        _java_method("com.macro.mall.dao.UmsRoleDao", "getMenuList"),
        _xml_statement("com.macro.mall.dao.UmsRoleDao", "getMenuList"),
    ])
    edges = synthesize_mybatis_java_xml_relations(facts)
    assert len(edges) == 1
    e = edges[0]
    assert e.type == RelationType.CALLS
    assert e.source_id.startswith("method//java-")
    assert e.target_id.startswith("method//xml-")
    assert e.attributes.get("synthesizedBy") == "mybatis-java-xml"
    assert e.attributes.get("provenance") == "heuristic"


def test_ambiguous_match_drops_conservatively():
    """两个不同包的同名 Java 类（重名）→ 不合成（保守丢弃）。"""
    facts = StructureFacts(entities=[
        _java_method("com.a.UserDao", "findById"),
        _java_method("com.b.UserDao", "findById"),  # 同 simpleName，不同包
        _xml_statement("com.a.UserDao", "findById"),
    ])
    edges = synthesize_mybatis_java_xml_relations(facts)
    assert len(edges) == 0


def test_no_java_match_skips_silently():
    """XML statement 找不到 Java 对应 method → 跳过，不报错也不合成。"""
    facts = StructureFacts(entities=[
        _xml_statement("com.x.OrphanDao", "queryAll"),
        # 故意不放 Java method
    ])
    edges = synthesize_mybatis_java_xml_relations(facts)
    assert len(edges) == 0


def test_edge_metadata_contains_via():
    """合成的 edge metadata 含 'via' 字段（className.id 形式）。"""
    facts = StructureFacts(entities=[
        _java_method("com.x.FooDao", "doIt"),
        _xml_statement("com.x.FooDao", "doIt"),
    ])
    edges = synthesize_mybatis_java_xml_relations(facts)
    assert edges[0].attributes.get("via") == "FooDao.doIt"


def test_multiple_xml_statements_match_in_one_pass():
    """一个 mapper 的多个 statement 同时匹配。"""
    facts = StructureFacts(entities=[
        _java_method("com.x.RoleDao", "getMenuList"),
        _java_method("com.x.RoleDao", "getResourceList"),
        _xml_statement("com.x.RoleDao", "getMenuList"),
        _xml_statement("com.x.RoleDao", "getResourceList"),
    ])
    edges = synthesize_mybatis_java_xml_relations(facts)
    assert len(edges) == 2
