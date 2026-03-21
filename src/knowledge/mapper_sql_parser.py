"""从 MyBatis Mapper XML 解析每个方法的 SQL，提取表与列的访问（读/写）。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from xml.etree import ElementTree as ET


@dataclass
class TableAccess:
    table: str
    op: str  # "select" | "insert" | "update" | "delete"
    columns: list[str]  # 能解析到的列，可为空
    sql_snippet: str = ""  # 该语句原始 SQL 片段（截断），供 UI 展示


@dataclass
class MapperMethodAccess:
    namespace: str  # Mapper 接口全限定名
    method_id: str  # XML 中 id 属性
    accesses: list[TableAccess]


# SQL 表名提取：FROM、JOIN、INTO、UPDATE、TABLE
_TABLE_PATTERN = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+`?(\w+)`?",
    re.IGNORECASE,
)
# 列名：SELECT a, b 或 SET a= 或 (a,b,c) VALUES
_COL_SELECT = re.compile(r"SELECT\s+(.+?)\s+FROM", re.IGNORECASE | re.DOTALL)
_COL_SET = re.compile(r"SET\s+(.+?)(?:WHERE|$)", re.IGNORECASE | re.DOTALL)
_COL_INSERT_COLS = re.compile(r"INSERT\s+INTO\s+\w+\s*\((.+?)\)", re.IGNORECASE | re.DOTALL)


def _extract_tables_from_sql(sql: str) -> list[str]:
    """从 SQL 文本提取表名。"""
    if not sql or not sql.strip():
        return []
    found: set[str] = set()
    for m in _TABLE_PATTERN.finditer(sql):
        t = (m.group(1) or "").strip()
        if t and t.upper() not in ("SELECT", "WHERE", "AND", "OR", "ON"):
            found.add(t)
    return list(found)


def _extract_columns_from_sql(sql: str, op: str) -> list[str]:
    """简单提取列名，不追求完备。"""
    cols: list[str] = []
    sql_norm = " ".join(sql.split())
    if op.lower() == "select":
        ma = _COL_SELECT.search(sql_norm)
        if ma:
            part = ma.group(1) or ""
            if "count(" in part.lower() or "*" in part:
                return []
            for m in re.finditer(r"`?(\w+)`?", part):
                c = m.group(1)
                if c.upper() not in ("AS", "DISTINCT", "FROM"):
                    cols.append(c)
    elif op.lower() == "update":
        ma = _COL_SET.search(sql_norm)
        if ma:
            part = ma.group(1) or ""
            for m in re.finditer(r"`?(\w+)`?\s*=", part):
                cols.append(m.group(1))
    elif op.lower() == "insert":
        ma = _COL_INSERT_COLS.search(sql_norm)
        if ma:
            part = ma.group(1) or ""
            for m in re.finditer(r"`?(\w+)`?", part):
                c = m.group(1)
                if c.upper() not in ("VALUES",):
                    cols.append(c)
    return cols[:50]


def _truncate_sql(sql: str, max_len: int = 1200) -> str:
    s = " ".join((sql or "").split())
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def _get_sql_text(elem: ET.Element) -> str:
    """获取元素内的 SQL 文本，处理 CDATA 和 include。"""
    if elem.text:
        return (elem.text or "").strip()
    parts: list[str] = []
    for c in elem:
        if c.text:
            parts.append(c.text)
        if c.tail:
            parts.append(c.tail)
    return " ".join(parts).strip()


def parse_mapper_xml(path: Path) -> list[MapperMethodAccess]:
    """
    解析单个 Mapper XML，返回该文件中每个 SQL 方法的表访问。
    """
    results: list[MapperMethodAccess] = []
    try:
        tree = ET.parse(path)
        root = tree.getroot()
    except Exception:
        return results

    namespace_attr = root.get("namespace") or ""
    namespace = (namespace_attr or "").strip()
    if not namespace:
        return results

    for tag in ("select", "insert", "update", "delete"):
        for elem in root.findall(f".//{tag}"):
            mid = (elem.get("id") or "").strip()
            if not mid:
                continue
            sql = _get_sql_text(elem)
            tables = _extract_tables_from_sql(sql)
            if not tables:
                continue
            op = tag.lower()
            columns = _extract_columns_from_sql(sql, op)
            sql_snip = _truncate_sql(sql)
            accesses = [TableAccess(table=t, op=op, columns=columns, sql_snippet=sql_snip) for t in tables]
            results.append(
                MapperMethodAccess(
                    namespace=namespace,
                    method_id=mid,
                    accesses=accesses,
                )
            )
    return results


def load_mapper_accesses(repo_root: Path, mapper_glob: str = "**/mapper/*Mapper.xml") -> list[MapperMethodAccess]:
    """从仓库中按 glob 匹配所有 Mapper XML，解析并合并结果。"""
    root = Path(repo_root)
    all_results: list[MapperMethodAccess] = []
    for p in root.glob(mapper_glob):
        if p.is_file() and p.suffix.lower() == ".xml":
            all_results.extend(parse_mapper_xml(p))
    return all_results
