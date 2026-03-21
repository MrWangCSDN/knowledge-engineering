"""共享数据模型：代码输入源、结构事实、语义增强事实、领域知识等。"""
from .code_source import CodeInputSource, FileItem, ModuleItem
from .structure import (
    StructureFacts,
    StructureEntity,
    StructureRelation,
    EntityType,
    RelationType,
)
from .semantic import SemanticFacts, SemanticEntity, BusinessLink
from .domain import DomainKnowledge, BusinessDomain, ServiceDomainMapping

__all__ = [
    "CodeInputSource",
    "FileItem",
    "ModuleItem",
    "StructureFacts",
    "StructureEntity",
    "StructureRelation",
    "EntityType",
    "RelationType",
    "SemanticFacts",
    "SemanticEntity",
    "BusinessLink",
    "DomainKnowledge",
    "BusinessDomain",
    "ServiceDomainMapping",
]
