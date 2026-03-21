"""本体实体类型与关系类型的中文描述。"""
from __future__ import annotations

from src.models.structure import EntityType, RelationType


class OntologyLabels:
    """实体类型与关系类型的可读描述。"""

    _ENTITY_DESC = {
        EntityType.FILE: "文件",
        EntityType.MODULE: "模块",
        EntityType.PACKAGE: "包",
        EntityType.CLASS: "类",
        EntityType.INTERFACE: "接口",
        EntityType.METHOD: "方法",
        EntityType.FIELD: "字段",
        EntityType.PARAMETER: "参数",
        EntityType.SERVICE: "服务",
        EntityType.API_ENDPOINT: "API 端点",
    }

    _RELATION_DESC = {
        RelationType.CONTAINS: "包含（类→方法）",
        RelationType.CALLS: "调用",
        RelationType.EXTENDS: "继承",
        RelationType.IMPLEMENTS: "实现",
        RelationType.DEPENDS_ON: "依赖",
        RelationType.BELONGS_TO: "归属（方法→类，类→包）",
        RelationType.RELATES_TO: "关联（类→所在文件）",
        RelationType.ANNOTATED_BY: "被注解",
        RelationType.SERVICE_CALLS: "服务调用",
        RelationType.SERVICE_EXPOSES: "服务暴露 API",
        RelationType.BINDS_TO_SERVICE: "绑定到服务",
    }

    @classmethod
    def entity_type_desc(cls, et: EntityType) -> str:
        return cls._ENTITY_DESC.get(et, et.value)

    @classmethod
    def relation_type_desc(cls, rt: RelationType) -> str:
        return cls._RELATION_DESC.get(rt, rt.value)
