"""语义层输出：语义增强事实（结构实体 + 领域标签 + 业务概念链接）。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class BusinessLink(BaseModel):
    """代码实体与业务概念的关联。"""
    business_concept_id: str  # 业务域/业务能力/术语 ID
    link_type: str  # implemented_by, related_to, in_domain
    confidence: float = 1.0
    source: str = "rule"  # rule | model | manual
    evidence: Optional[str] = None  # 如 name_match, path_match


class SemanticEntity(BaseModel):
    """带语义增强的实体（基于结构实体扩展）。"""
    structure_entity_id: str  # 指向 StructureFacts.entities[].id
    domain_term_ids: list[str] = Field(default_factory=list)  # 识别到的领域术语
    business_links: list[BusinessLink] = Field(default_factory=list)
    embed_text: Optional[str] = None  # 用于向量化的文本
    vector_id: Optional[str] = None  # 写入向量库后的 ID（可选）


class SemanticFacts(BaseModel):
    """语义层输出：供知识层写入本体与图谱。"""
    semantic_entities: list[SemanticEntity] = Field(default_factory=list)
    meta: dict = Field(default_factory=dict)
