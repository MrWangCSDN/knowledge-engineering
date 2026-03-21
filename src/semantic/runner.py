"""语义层入口：结构事实 + 领域知识库 -> 语义增强事实。"""
from __future__ import annotations

import re
from src.models import (
    StructureFacts,
    SemanticFacts,
    SemanticEntity,
    BusinessLink,
    DomainKnowledge,
    EntityType,
    RelationType,
)


def run_semantic_layer(
    structure_facts: StructureFacts,
    domain: DomainKnowledge,
    enable_vector_text: bool = True,
) -> SemanticFacts:
    """
    在结构事实之上做领域术语识别与业务关联，输出语义增强事实。
    - 术语识别：命名、驼峰拆分、签名；可选扩展注释/注解。
    - 能力关联：path 匹配（含类级 path 前缀）、类名后缀启发（Controller/Service）。
    - embed_text 供知识层向量化与存储。
    """
    term_ids_by_name = _build_term_lookup(domain)
    capability_paths = _build_capability_paths(domain)
    class_id_by_entity: dict[str, str] = {}
    class_entities: dict[str, object] = {}
    for e in structure_facts.entities:
        if e.type in (EntityType.CLASS, EntityType.INTERFACE):
            class_entities[e.id] = e
    for e in structure_facts.entities:
        if e.type in (EntityType.METHOD, EntityType.FIELD):
            for r in structure_facts.relations:
                if r.type == RelationType.BELONGS_TO and r.source_id == e.id:
                    target_ent = structure_facts.entity_by_id(r.target_id)
                    if target_ent and target_ent.type in (EntityType.CLASS, EntityType.INTERFACE):
                        class_id_by_entity[e.id] = r.target_id
                    break

    semantic_entities: list[SemanticEntity] = []
    for e in structure_facts.entities:
        if e.type not in (EntityType.CLASS, EntityType.INTERFACE, EntityType.METHOD, EntityType.FIELD):
            continue
        text_for_terms = e.name + " " + (e.attributes.get("signature") or "")
        domain_term_ids = _match_terms_with_camel(text_for_terms, term_ids_by_name)
        business_links: list[BusinessLink] = []

        effective_path = (e.attributes or {}).get("path") or ""
        if e.type == EntityType.METHOD and e.id in class_id_by_entity:
            cls = structure_facts.entity_by_id(class_id_by_entity[e.id])
            if cls and (cls.attributes or {}).get("path"):
                prefix = (cls.attributes.get("path") or "").rstrip("/")
                effective_path = (prefix + "/" + effective_path) if effective_path else prefix
                effective_path = effective_path.strip("/")

        for cap_id, pattern in capability_paths:
            if pattern and re.search(pattern.replace("**", ".*"), effective_path):
                business_links.append(
                    BusinessLink(
                        business_concept_id=cap_id,
                        link_type="implemented_by",
                        source="rule",
                        evidence="path_match",
                        confidence=0.9,
                    )
                )
        if not business_links and e.type == EntityType.METHOD and _is_controller_like(e, class_entities, class_id_by_entity):
            for cap_id, pattern in capability_paths:
                if cap_id:
                    business_links.append(
                        BusinessLink(
                            business_concept_id=cap_id,
                            link_type="implemented_by",
                            source="rule",
                            evidence="class_naming",
                            confidence=0.5,
                        )
                    )
                    break

        for tid in domain_term_ids:
            business_links.append(
                BusinessLink(
                    business_concept_id=tid,
                    link_type="related_to",
                    source="rule",
                    evidence="name_match",
                    confidence=0.85,
                )
            )
        embed_text = None
        if enable_vector_text:
            embed_text = _embed_text_for_entity(e, structure_facts, class_entities, class_id_by_entity)
        semantic_entities.append(
            SemanticEntity(
                structure_entity_id=e.id,
                domain_term_ids=domain_term_ids,
                business_links=business_links,
                embed_text=embed_text,
            )
        )

    return SemanticFacts(semantic_entities=semantic_entities, meta={"domain_loaded": True})


def _is_controller_like(e, class_entities: dict, class_id_by_entity: dict) -> bool:
    if e.type != EntityType.METHOD:
        return False
    class_id = class_id_by_entity.get(e.id)
    if not class_id:
        return False
    cls = class_entities.get(class_id)
    if not cls:
        return False
    name = getattr(cls, "name", "") or ""
    return "Controller" in name or "Resource" in name or "Api" in name


def _build_term_lookup(domain: DomainKnowledge) -> dict[str, str]:
    """术语名/同义词 -> term id。"""
    lookup: dict[str, str] = {}
    for t in domain.terms:
        tid = t.get("id") or ""
        name = (t.get("name") or "").lower()
        if name:
            lookup[name] = tid
        for s in t.get("synonyms") or []:
            lookup[str(s).lower()] = tid
    return lookup


def _build_capability_paths(domain: DomainKnowledge) -> list[tuple[str, str]]:
    """(capability_id, path_pattern) 列表。"""
    out: list[tuple[str, str]] = []
    for c in domain.capabilities:
        cid = c.get("id") or ""
        pattern = c.get("path_pattern") or ""
        if cid:
            out.append((cid, pattern))
    return out


def _camel_tokens(s: str) -> list[str]:
    """驼峰与下划线拆分：OrderController -> [order, controller], get_order_id -> [get, order, id]。"""
    s = re.sub(r"_", " ", s)
    parts = re.findall(r"[A-Za-z][a-z0-9]*|[A-Z]+(?=[A-Z][a-z]|\d|\W|$)|[A-Z]", s)
    return [p.lower() for p in parts if p]


def _match_terms_with_camel(text: str, term_ids_by_name: dict[str, str]) -> list[str]:
    """命名+签名中匹配领域术语（含驼峰拆分）。"""
    seen: set[str] = set()
    ids: list[str] = []
    tokens = re.findall(r"[A-Za-z][a-zA-Z0-9]*", text)
    for t in tokens:
        tid = term_ids_by_name.get(t.lower())
        if tid and tid not in seen:
            seen.add(tid)
            ids.append(tid)
    for t in tokens:
        for subtoken in _camel_tokens(t):
            tid = term_ids_by_name.get(subtoken)
            if tid and tid not in seen:
                seen.add(tid)
                ids.append(tid)
    return ids


def _embed_text_for_entity(e, structure_facts: StructureFacts, class_entities: dict, class_id_by_entity: dict) -> str:
    """拼出用于向量化的文本：类名、方法名、签名、path、位置。"""
    parts = [e.name, e.type.value]
    if e.type == EntityType.METHOD and e.id in class_id_by_entity:
        cls = structure_facts.entity_by_id(class_id_by_entity[e.id])
        if cls:
            parts.insert(0, cls.name)
    if e.location:
        parts.append(e.location)
    if (e.attributes or {}).get("signature"):
        parts.append(e.attributes["signature"])
    if (e.attributes or {}).get("path"):
        parts.append(e.attributes["path"])
    return " ".join(str(p) for p in parts)
