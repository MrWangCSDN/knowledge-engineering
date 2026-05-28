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


def test_java_method_without_explicit_language_is_recognized(monkeypatch):
    """关键回归：javaparser-bridge 不设 e.language 时，仍能识别为 Java 候选。

    Bug 历史：原 link_resolver 用 `language in (java, kotlin)` 过滤，
    而 javaparser-bridge 当前不显式设 language，导致 mall-swarm 实测 Java→XML 边 = 0。
    Fix 后：用"非 xml = Java 候选"反向判断，empty language 也能被识别。
    """
    # 构造 entity 但显式把 language 设为空字符串（模拟 javaparser-bridge 实际行为）
    java_no_lang = StructureEntity(
        id="method//java-no-lang",
        type=EntityType.METHOD,
        name="getMenuList",
        language="",  # ← 关键：模拟实际 java extractor 不设 language 的行为
        attributes={"qualified_name": "com.macro.mall.dao.UmsRoleDao::getMenuList"},
    )
    xml_match = _xml_statement("com.macro.mall.dao.UmsRoleDao", "getMenuList")
    facts = StructureFacts(entities=[java_no_lang, xml_match])

    edges = synthesize_mybatis_java_xml_relations(facts)
    # fix 前会拿到 0 边；fix 后应拿到 1 边
    assert len(edges) == 1, (
        f"empty-language Java method 应当被识别为 Java 候选；"
        f"实际 edges={len(edges)}（fix 失效）"
    )
    assert edges[0].source_id == "method//java-no-lang"
    assert edges[0].target_id == xml_match.id
    assert edges[0].attributes.get("synthesizedBy") == "mybatis-java-xml"


def test_xml_explicit_language_not_treated_as_java():
    """language='xml' 的 method 不应被加进 Java 候选索引（防自连）。"""
    # 两个 XML statement 撞名（理论上不会发生，但作为安全网保留）
    xml1 = _xml_statement("com.x.RoleDao", "getMenuList")
    xml2 = _xml_statement("com.y.RoleDao", "getMenuList")
    facts = StructureFacts(entities=[xml1, xml2])

    edges = synthesize_mybatis_java_xml_relations(facts)
    # 两个都是 xml，java_index 为空，不应合成任何边
    assert len(edges) == 0
