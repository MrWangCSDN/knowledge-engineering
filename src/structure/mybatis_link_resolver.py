"""MyBatis Java↔XML 关联 — 把 Java mapper interface method 跟 XML statement 连起来。

设计：[[MyBatis-XML-Extractor-设计]] §4
参考：CodeGraph callback-synthesizer.ts mybatisJavaXmlEdges() (62 行)

算法：
1. 索引所有 Java/Kotlin method entity by `<ClassName>::<methodName>`
2. 对每个 language=xml method entity：
   a. 从 qualified_name 拆 namespace + id
   b. className = namespace 最后一段（点分割）
   c. 在 Java 索引按 <className>::<id> 查
   d. 唯一匹配 → emit StructureRelation(type=CALLS,
      attributes={synthesizedBy: "mybatis-java-xml", provenance: "heuristic", via: ...})
   e. 多匹配（包名歧义）→ 保守丢弃
   f. 0 匹配 → 跳过

为什么"保守丢弃"：mall-swarm 实测 namespace 都是唯一的，
误连边比缺连边更影响下游（ke_callees / ke_impact 会跑到错误链路）。
"""
from __future__ import annotations

from src.models.structure import (
    EntityType,
    RelationType,
    StructureEntity,
    StructureFacts,
    StructureRelation,
)


def synthesize_mybatis_java_xml_relations(
    facts: StructureFacts,
) -> list[StructureRelation]:
    """合成 Java method → XML statement 的 CALLS relation 列表。

    :param facts: StructureFacts，含 Java + XML 两种 method entity
    :returns: 新合成的 CALLS relation 列表（不会直接 append 到 facts，
              留给调用方控制时机）
    """
    # 1. 索引 Java/Kotlin method by <ClassName>::<methodName>
    java_index: dict[str, list[StructureEntity]] = {}
    for e in facts.entities:
        if e.type != EntityType.METHOD:
            continue
        if (e.language or "").lower() not in ("java", "kotlin"):
            continue
        qn = e.attributes.get("qualified_name", "")
        parts = qn.split("::")
        if len(parts) < 2:
            continue
        class_fqn = parts[-2]      # 如 "com.x.UserDao"
        method_name = parts[-1]    # 如 "findById"
        class_name = class_fqn.split(".")[-1]  # "UserDao"
        key = f"{class_name}::{method_name}"
        java_index.setdefault(key, []).append(e)

    # 2. 遍历 XML method 匹配
    new_edges: list[StructureRelation] = []
    seen: set[str] = set()
    for xml in facts.entities:
        if xml.type != EntityType.METHOD:
            continue
        if (xml.language or "").lower() != "xml":
            continue
        qn = xml.attributes.get("qualified_name", "")
        if "::" not in qn:
            continue
        namespace, statement_id = qn.rsplit("::", 1)
        if not namespace or not statement_id:
            continue
        class_name = namespace.split(".")[-1]
        candidates = java_index.get(f"{class_name}::{statement_id}", [])

        # 保守策略：多匹配丢弃；0 匹配跳过
        if len(candidates) != 1:
            continue
        java = candidates[0]

        # 去重（同一对 java/xml 只连一次）
        key = f"{java.id}->{xml.id}"
        if key in seen:
            continue
        seen.add(key)

        new_edges.append(StructureRelation(
            type=RelationType.CALLS,
            source_id=java.id,
            target_id=xml.id,
            attributes={
                "synthesizedBy": "mybatis-java-xml",
                "provenance": "heuristic",
                "via": f"{class_name}.{statement_id}",
            },
        ))

    return new_edges
