"""方法↔表访问相关数据类型。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TableAccessDetail:
    table: str
    op: str  # "select" | "insert" | "update" | "delete"
    columns: list[str]
    source_method_id: str
    hop: int
    path_method_ids: list[str]
    mapper_statement: str
    sql_snippet: str = ""


@dataclass
class TableAccessGrouped:
    table: str
    op: str
    min_hop: int
    max_hop: int
    items: list[TableAccessDetail]


@dataclass
class MethodAccessResult:
    read_groups: list[TableAccessGrouped] = field(default_factory=list)
    write_groups: list[TableAccessGrouped] = field(default_factory=list)


@dataclass
class MethodForTable:
    method_id: str
    op: str
    hop: int
    source_method_id: str
