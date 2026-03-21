"""结构层输出：与语言无关的「结构事实」模型。"""
from __future__ import annotations

from enum import Enum
from typing import Optional, Any

from pydantic import BaseModel, Field


class EntityType(str, Enum):
    """代码本体实体类型。"""
    FILE = "file"
    MODULE = "module"
    PACKAGE = "package"
    CLASS = "class"
    INTERFACE = "interface"
    METHOD = "method"
    FIELD = "field"
    PARAMETER = "parameter"
    # 微服务可选
    SERVICE = "service"
    API_ENDPOINT = "api_endpoint"


class RelationType(str, Enum):
    """结构关系类型。"""
    CONTAINS = "contains"           # 包含：类→方法（文件与包无关系）
    CALLS = "calls"                # 调用
    EXTENDS = "extends"            # 继承
    IMPLEMENTS = "implements"      # 实现
    DEPENDS_ON = "depends_on"      # 依赖
    BELONGS_TO = "belongs_to"      # 归属：方法→类，类→包（包与模块无关系）
    RELATES_TO = "relates_to"      # 关联：类→文件（类所在文件）
    ANNOTATED_BY = "annotated_by"  # 被注解
    # 微服务可选
    SERVICE_CALLS = "service_calls"    # 服务—调用—服务
    SERVICE_EXPOSES = "service_exposes"  # 服务—暴露—API
    BINDS_TO_SERVICE = "binds_to_service"  # Feign 接口—绑定—被调服务


class StructureEntity(BaseModel):
    """结构事实中的实体。"""
    id: str  # canonical_v1：file://相对路径、class//method// 为 sha256 确定性短哈希，跨次构建稳定（见 structure 层 meta）
    type: EntityType
    name: str
    location: Optional[str] = None  # 文件:行 或 文件:行-行
    module_id: Optional[str] = None  # 所属模块/服务
    language: Optional[str] = None
    attributes: dict[str, Any] = Field(default_factory=dict)  # 如 signature, visibility, path(API)


class StructureRelation(BaseModel):
    """结构事实中的关系。"""
    type: RelationType
    source_id: str
    target_id: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class StructureFacts(BaseModel):
    """结构层输出：结构事实库。"""
    entities: list[StructureEntity] = Field(default_factory=list)
    relations: list[StructureRelation] = Field(default_factory=list)
    meta: dict = Field(default_factory=dict)  # 如 repo_version, parsed_at

    def entity_by_id(self, eid: str) -> StructureEntity | None:
        for e in self.entities:
            if e.id == eid:
                return e
        return None

    def relations_from(self, source_id: str, rel_type: RelationType | None = None) -> list[StructureRelation]:
        out = [r for r in self.relations if r.source_id == source_id]
        if rel_type is not None:
            out = [r for r in out if r.type == rel_type]
        return out

    def relations_to(self, target_id: str, rel_type: RelationType | None = None) -> list[StructureRelation]:
        out = [r for r in self.relations if r.target_id == target_id]
        if rel_type is not None:
            out = [r for r in out if r.type == rel_type]
        return out
