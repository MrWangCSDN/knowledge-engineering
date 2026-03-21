"""Mapper 方法→表访问索引：DDL + Mapper 解析、method_id 解析与模板匹配。"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from src.knowledge.ddl_parser import TableInfo, load_ddl_from_file
from src.knowledge.mapper_sql_parser import MapperMethodAccess, load_mapper_accesses
from src.knowledge.method_entity_id_normalize import (
    method_entity_id_variants,
    normalize_method_entity_id,
)
from src.knowledge.method_table_types import TableAccessDetail


def _resolve_mapper_to_method_id(
    namespace: str, method_id: str, backend: Any
) -> str | None:
    if not backend:
        return None
    class_simple = namespace.rsplit(".", 1)[-1] if "." in namespace else namespace
    search_fn = getattr(backend, "search_by_name", None)
    if not search_fn:
        return None
    try:
        hits = search_fn(method_id, entity_types=["method"], limit=200)
    except TypeError:
        try:
            hits = search_fn(method_id, entity_types=["method"])
        except Exception:
            return None
    except Exception:
        return None
    for h in hits or []:
        cn = str(h.get("class_name") or "").strip()
        if class_simple in cn or cn.endswith("." + class_simple):
            return str(h.get("id") or "")
    return None


def _short_mapper_name(namespace: str) -> str:
    return namespace.rsplit(".", 1)[-1] if "." in namespace else namespace


def _mapper_simple_class_from_location(loc: str) -> str:
    s = (loc or "").replace("\\", "/")
    m = re.search(r"/([\w]+Mapper)\.java:", s, re.IGNORECASE)
    if m:
        return m.group(1).lower()
    m2 = re.search(r"([\w]+Mapper)\.java", s, re.IGNORECASE)
    return m2.group(1).lower() if m2 else ""


def _method_simple_name_from_graph_node(n: dict) -> str:
    raw = str(n.get("name") or "").strip()
    if raw:
        return raw.lower()
    sig = str(n.get("signature") or "").strip()
    if not sig or "(" not in sig:
        return ""
    head = sig.split("(", 1)[0].strip()
    if not head:
        return ""
    parts = head.replace("@", " ").split()
    token = parts[-1] if parts else head
    token = token.split(".")[-1]
    return token.strip().lower()


def _get_node_with_variants(backend: Any, mid: str) -> dict | None:
    get_fn = getattr(backend, "get_node", None)
    if not callable(get_fn):
        return None
    for vid in method_entity_id_variants(mid) or [mid]:
        try:
            n = get_fn(vid)
            if n:
                return dict(n) if hasattr(n, "keys") else n
        except Exception:
            continue
    return None


class MapperAccessIndex:
    """DDL + Mapper 解析结果与方法↔表访问索引。"""

    def __init__(self, repo_root: Path, ddl_path: str, mapper_glob: str) -> None:
        self.repo_root = Path(repo_root)
        self.ddl_path = ddl_path
        self.mapper_glob = mapper_glob
        self._tables: list[TableInfo] = []
        self._tables_by_name: dict[str, TableInfo] = {}
        self._mapper_accesses: list[MapperMethodAccess] = []
        self._ns_id_to_method: dict[tuple[str, str], str] = {}
        self._method_direct: dict[str, list[TableAccessDetail]] = defaultdict(list)
        self._method_direct_by_pair: dict[
            tuple[str, str], list[TableAccessDetail]
        ] = defaultdict(list)
        self._table_to_methods: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        self._loaded = False

    def load(self) -> None:
        if self._loaded:
            return
        ddl_file = self.repo_root / self.ddl_path
        self._tables = load_ddl_from_file(ddl_file)
        self._tables_by_name = {t.name: t for t in self._tables}
        self._mapper_accesses = load_mapper_accesses(self.repo_root, self.mapper_glob)
        for ma in self._mapper_accesses:
            for acc in ma.accesses:
                self._table_to_methods[acc.table].append(
                    (ma.namespace, ma.method_id, acc.op)
                )
        self._loaded = True

    def resolve_mapper_methods(self, backend: Any) -> None:
        self.load()
        self._ns_id_to_method.clear()
        self._method_direct.clear()
        self._method_direct_by_pair.clear()
        for ma in self._mapper_accesses:
            mid = _resolve_mapper_to_method_id(ma.namespace, ma.method_id, backend)
            stmt = f"{_short_mapper_name(ma.namespace)}.{ma.method_id}"
            short_l = _short_mapper_name(ma.namespace).strip().lower()
            stmt_l = (ma.method_id or "").strip().lower()
            pair_key = (short_l, stmt_l)
            if mid:
                self._ns_id_to_method[(ma.namespace, ma.method_id)] = mid
            mid_norm = normalize_method_entity_id(mid) if mid else ""
            for acc in ma.accesses:
                cols = getattr(acc, "columns", None) or []
                if isinstance(cols, str):
                    cols = [cols]
                sql_snip = str(getattr(acc, "sql_snippet", "") or "")
                detail = TableAccessDetail(
                    table=acc.table,
                    op=acc.op,
                    columns=list(cols)[:30],
                    source_method_id=mid or "",
                    hop=0,
                    path_method_ids=[mid] if mid else [],
                    mapper_statement=stmt,
                    sql_snippet=sql_snip,
                )
                self._method_direct_by_pair[pair_key].append(detail)
                if mid and mid_norm:
                    self._method_direct[mid_norm].append(detail)

    def templates_for_bfs_method(
        self,
        backend: Any,
        merge_backend: Any | None,
        mid: str,
    ) -> list[TableAccessDetail]:
        mid_key = normalize_method_entity_id(mid)
        seen: set[int] = set()
        out: list[TableAccessDetail] = []
        for t in self._method_direct.get(mid_key, []):
            if id(t) not in seen:
                seen.add(id(t))
                out.append(t)
        if mid and mid != mid_key:
            for t in self._method_direct.get(mid, []):
                if id(t) not in seen:
                    seen.add(id(t))
                    out.append(t)
        n = None
        for b in (backend, merge_backend):
            if b is None:
                continue
            n = _get_node_with_variants(b, mid)
            if n:
                break
        mid_l = (mid or "").strip().lower()
        is_methodish = mid_l.startswith("method://") or mid_l.startswith("method//")
        et = str((n or {}).get("entity_type") or "").lower()
        if n and (et == "method" or is_methodish):
            cn = str(n.get("class_name") or "")
            simple = cn.rsplit(".", 1)[-1].strip().lower()
            if not simple:
                simple = _mapper_simple_class_from_location(
                    str(n.get("location") or "")
                )
            mname = _method_simple_name_from_graph_node(n)
            if simple and mname:
                for t in self._method_direct_by_pair.get((simple, mname), []):
                    if id(t) not in seen:
                        seen.add(id(t))
                        out.append(t)
        return out

    def tables(self) -> list[str]:
        self.load()
        return [t.name for t in self._tables]

    def tables_sorted(self) -> list[str]:
        return sorted(self.tables())

    def table_schema_text(self, table_name: str, max_cols: int = 40) -> str:
        self.load()
        t = self._tables_by_name.get(table_name)
        if not t:
            return f"（DDL 中未找到表 `{table_name}`）"
        lines = [f"表 `{t.name}`（{len(t.columns)} 列）"]
        for c in t.columns[:max_cols]:
            lines.append(f"  · `{c.name}` {c.type_info}")
        if len(t.columns) > max_cols:
            lines.append(f"  … 共 {len(t.columns)} 列，仅展示前 {max_cols} 个")
        return "\n".join(lines)

    @property
    def table_to_methods(self) -> dict[str, list[tuple[str, str, str]]]:
        self.load()
        return self._table_to_methods

    @property
    def ns_id_to_method(self) -> dict[tuple[str, str], str]:
        return self._ns_id_to_method
