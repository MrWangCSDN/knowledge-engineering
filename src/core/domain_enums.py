"""
解读流水线、UI 与存储层共用的领域字面量（避免魔串散落）。

与 Weaviate、JSON 持久化交互时统一使用 ``.value``。
"""
from __future__ import annotations

from enum import Enum


class InterpretPhase(str, Enum):
    """技术解读 / 业务解读阶段键（如 ``interp_stats``、进度诊断）。"""

    TECH = "tech"
    BIZ = "biz"


class BusinessInterpretLevel(str, Enum):
    """业务解读在 Weaviate 中的 level 字段。"""

    API = "api"
    CLASS = "class"
    MODULE = "module"


# 解读专区支持的图谱实体类型（小写字符串，与 Neo4j 节点 entity_type 对齐）
INTERP_PANEL_ENTITY_TYPES: frozenset[str] = frozenset({"method", "class", "interface"})
