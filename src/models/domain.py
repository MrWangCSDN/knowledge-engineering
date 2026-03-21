"""领域知识库与「服务/模块—业务域」映射（配置驱动）。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class BusinessDomain(BaseModel):
    """业务域（限界上下文）。"""
    id: str
    name: Optional[str] = None
    description: Optional[str] = None
    capability_ids: list[str] = Field(default_factory=list)  # 该域下业务能力
    term_ids: list[str] = Field(default_factory=list)  # 术语/同义词


class ServiceDomainMapping(BaseModel):
    """服务/模块 — 承载 — 业务域 映射。"""
    service_or_module_id: str
    business_domain_ids: list[str]
    weight: float = 1.0  # 主承载=1.0，次要=0.5


class DomainKnowledge(BaseModel):
    """领域知识库：业务域、业务能力、术语。"""
    business_domains: list[BusinessDomain] = Field(default_factory=list)
    capabilities: list[dict] = Field(default_factory=list)  # [{id, name, path_pattern?}]
    terms: list[dict] = Field(default_factory=list)  # [{id, name, synonyms?: []}]
    service_domain_mappings: list[ServiceDomainMapping] = Field(default_factory=list)
