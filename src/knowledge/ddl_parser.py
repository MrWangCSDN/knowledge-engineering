"""从 MySQL DDL（CREATE TABLE）解析表与列结构。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ColumnInfo:
    name: str
    type_info: str  # 原始类型描述，如 bigint(20), varchar(100)


@dataclass
class TableInfo:
    name: str
    columns: list[ColumnInfo]
    comment: str = ""


def parse_ddl_sql(content: str) -> list[TableInfo]:
    """
    解析 MySQL DDL 文本，提取 CREATE TABLE 定义。
    返回 TableInfo 列表；列名从反引号或首词提取。
    使用括号匹配处理类型中的括号（如 bigint(20)）。
    """
    tables: list[TableInfo] = []
    create_re = re.compile(r"CREATE\s+TABLE\s+`?(\w+)`?\s*\(", re.IGNORECASE)
    for m in create_re.finditer(content):
        table_name = (m.group(1) or "").strip()
        if not table_name:
            continue
        start = m.end()
        depth = 1
        pos = start
        while pos < len(content) and depth > 0:
            c = content[pos]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            pos += 1
        if depth == 0:
            body = content[start : pos - 1]
            columns = _parse_columns(body)
            tables.append(TableInfo(name=table_name, columns=columns, comment=""))
    return tables


def _parse_columns(body: str) -> list[ColumnInfo]:
    """从 CREATE TABLE 括号内解析列定义。"""
    columns: list[ColumnInfo] = []
    # 按逗号拆分，但要跳过括号内的逗号
    depth = 0
    start = 0
    for i, c in enumerate(body):
        if c in "(":
            depth += 1
        elif c in ")":
            depth -= 1
        elif c == "," and depth == 0:
            part = body[start:i].strip()
            col = _parse_column_line(part)
            if col:
                columns.append(col)
            start = i + 1
    if start < len(body):
        part = body[start:].strip()
        col = _parse_column_line(part)
        if col:
            columns.append(col)
    return columns


def _parse_column_line(line: str) -> ColumnInfo | None:
    """解析单行列定义：`col` type ... 或 col type ..."""
    line = line.strip()
    if not line or line.upper().startswith(("PRIMARY", "KEY", "UNIQUE", "INDEX", "CONSTRAINT", "FOREIGN")):
        return None

    # `name` 或 name 开头
    col_match = re.match(r"^`?(\w+)`?\s+(\w+(?:\([^)]*\))?)", line, re.IGNORECASE)
    if col_match:
        return ColumnInfo(name=col_match.group(1), type_info=col_match.group(2))
    return None


def load_ddl_from_file(path: Path) -> list[TableInfo]:
    """从文件加载并解析 DDL。"""
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return parse_ddl_sql(text)
    except Exception:
        return []
