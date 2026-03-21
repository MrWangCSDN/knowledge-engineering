"""方法-表访问服务：编排 MapperAccessIndex 与 MethodTableGraphWalker，支持方法查表、表查方法。"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from src.knowledge.mapper_access_index import MapperAccessIndex
from src.knowledge.method_table_graph_walker import (
    GraphWalkPredecessorConfig,
    GraphWalkSuccessorConfig,
    bfs_min_hops,
    filter_ids_excluding_prefixes as _filter_ids_excluding_prefixes,
    merged_predecessors_for_walk,
    merged_successors_for_walk,
    reconstruct_path,
    resolve_bfs_start_id,
    safe_predecessors_for_walk,
    safe_successors_for_walk,
)
from src.knowledge.method_table_types import (
    MethodAccessResult,
    MethodForTable,
    TableAccessDetail,
    TableAccessGrouped,
)

# 向后兼容：表访问场景从本模块导入类型
__all__ = [
    "TableAccessDetail",
    "TableAccessGrouped",
    "MethodAccessResult",
    "MethodForTable",
    "MethodTableAccessService",
    "format_method_table_debug_report",
]


def _is_method_node(backend: Any, nid: str) -> bool:
    if not nid:
        return False
    s = (nid or "").strip().lower()
    if s.startswith("method://") or s.startswith("method//"):
        return True
    if backend and hasattr(backend, "get_node"):
        try:
            n = backend.get_node(nid)
        except Exception:
            n = None
        if n and str(n.get("entity_type") or "").lower() == "method":
            return True
    return False


def format_method_table_debug_report(
    *,
    backend: Any,
    merge_backend: Any | None,
    svc: "MethodTableAccessService",
    start_method_id: str,
    max_hops: int,
    repo_cfg: dict[str, Any] | None = None,
    successor_walk: GraphWalkSuccessorConfig | None = None,
    knowledge_graph_backend: str | None = None,
) -> str:
    walk = successor_walk or GraphWalkSuccessorConfig.method_to_table_default()
    lines: list[str] = []
    mode = (
        "仅 calls"
        if walk.calls_only
        else "全部出边（已排除 implements；已过滤 term/domain/capability 目标）"
    )
    lines.append(f"方法查表 BFS 模式: {mode} · 后端: {type(backend).__name__}")
    lines.append(f"合并后继来源: {type(merge_backend).__name__ if merge_backend else '无'}")
    start = resolve_bfs_start_id(backend, merge_backend, start_method_id)
    sid_show = start if len(start) < 200 else start[:197] + "..."
    lines.append(f"起点 id（规范化）: {sid_show}")
    s0 = safe_successors_for_walk(backend, start, walk)
    lines.append(f"主后端起点直接后继数: {len(s0)}")
    if merge_backend:
        sm = merged_successors_for_walk(backend, merge_backend, start, walk)
        lines.append(f"合并后起点直接后继数: {len(sm)}")
    best, _ = bfs_min_hops(
        start,
        backend,
        max_hops,
        merge_backend=merge_backend,
        successor_walk=walk,
    )
    lines.append(f"BFS 可达节点数（含起点）: {len(best)}")
    index = svc._index
    lines.append(f"Mapper→method 绑定数: {len(index.ns_id_to_method)}")
    hit_mapper = sum(
        1
        for mid in best
        if index.templates_for_bfs_method(backend, merge_backend, mid)
    )
    lines.append(f"其中已映射 Mapper SQL 的节点数: {hit_mapper}")
    kb = (knowledge_graph_backend or "").strip() or None
    if kb is None and isinstance(repo_cfg, dict):
        gc = ((repo_cfg.get("knowledge") or {}).get("graph") or {})
        _g = str(gc.get("backend") or "").strip()
        kb = _g or None
    if kb is not None:
        lines.append(f"project.yaml knowledge.graph.backend: {kb!r}")
    return "\n".join(lines)


def _group_by_table_op(details: list[TableAccessDetail]) -> list[TableAccessGrouped]:
    key_map: dict[tuple[str, str], list[TableAccessDetail]] = defaultdict(list)
    for d in details:
        key_map[(d.table, d.op)].append(d)
    groups: list[TableAccessGrouped] = []
    for (table, op), items in key_map.items():
        items = sorted(items, key=lambda x: (x.hop, x.mapper_statement, x.source_method_id))
        min_hop = min(x.hop for x in items)
        max_hop = max(x.hop for x in items)
        groups.append(
            TableAccessGrouped(table=table, op=op, min_hop=min_hop, max_hop=max_hop, items=items)
        )
    groups.sort(key=lambda g: (g.min_hop, g.table, g.op))
    return groups


class MethodTableAccessService:
    """编排 MapperAccessIndex 与 MethodTableGraphWalker，对外提供方法↔表查询。"""

    def __init__(self, repo_root: Path, ddl_path: str, mapper_glob: str) -> None:
        self.repo_root = Path(repo_root)
        self.ddl_path = ddl_path
        self.mapper_glob = mapper_glob
        self._index = MapperAccessIndex(repo_root, ddl_path, mapper_glob)

    def load(self) -> None:
        self._index.load()

    def resolve_mapper_methods(self, backend: Any) -> None:
        self._index.resolve_mapper_methods(backend)

    def templates_for_bfs_method(
        self,
        backend: Any,
        merge_backend: Any | None,
        mid: str,
    ) -> list[TableAccessDetail]:
        return self._index.templates_for_bfs_method(backend, merge_backend, mid)

    def table_schema_text(self, table_name: str, max_cols: int = 40) -> str:
        return self._index.table_schema_text(table_name, max_cols)

    def tables(self) -> list[str]:
        return self._index.tables()

    def tables_sorted(self) -> list[str]:
        return self._index.tables_sorted()

    def get_tables_for_method(
        self,
        method_id: str,
        backend: Any,
        max_hops: int = 8,
        merge_backend: Any | None = None,
    ) -> MethodAccessResult:
        self.load()
        self.resolve_mapper_methods(backend)
        read_list: list[TableAccessDetail] = []
        write_list: list[TableAccessDetail] = []
        if not backend:
            return MethodAccessResult()

        start = resolve_bfs_start_id(backend, merge_backend, method_id)
        mb = merge_backend if merge_backend is not backend else None
        best_hop, parent = bfs_min_hops(
            start,
            backend,
            max_hops,
            merge_backend=mb,
            successor_walk=GraphWalkSuccessorConfig.method_to_table_default(),
        )

        for mid, hop in best_hop.items():
            for tmpl in self._index.templates_for_bfs_method(backend, mb, mid):
                path = reconstruct_path(parent, start, mid)
                d2 = TableAccessDetail(
                    table=tmpl.table,
                    op=tmpl.op,
                    columns=list(tmpl.columns),
                    source_method_id=mid,
                    hop=hop,
                    path_method_ids=path,
                    mapper_statement=tmpl.mapper_statement,
                    sql_snippet=tmpl.sql_snippet,
                )
                if tmpl.op == "select":
                    read_list.append(d2)
                else:
                    write_list.append(d2)

        return MethodAccessResult(
            read_groups=_group_by_table_op(read_list),
            write_groups=_group_by_table_op(write_list),
        )

    def get_methods_for_table(
        self,
        table_name: str,
        backend: Any,
        op_filter: str | None = None,
        max_hops: int = 8,
        merge_backend: Any | None = None,
    ) -> list[MethodForTable]:
        self.load()
        self.resolve_mapper_methods(backend)
        mb0 = merge_backend if merge_backend is not backend else None
        pred_walk = GraphWalkPredecessorConfig.table_to_method_default()
        table_to_methods = self._index.table_to_methods
        candidates: list[tuple[str, str, int]] = []
        for ns, mid_str, op in table_to_methods.get(table_name, []):
            graph_mid = self._index.ns_id_to_method.get((ns, mid_str))
            if not graph_mid:
                continue
            if not (
                _is_method_node(backend, graph_mid)
                or (mb0 is not None and _is_method_node(mb0, graph_mid))
            ):
                continue
            if op_filter == "read" and op != "select":
                continue
            if op_filter == "write" and op in ("select",):
                continue
            candidates.append((graph_mid, op, 0))
        if not backend:
            return [
                MethodForTable(method_id=m, op=o, hop=h, source_method_id=m)
                for m, o, h in candidates
            ]
        result: list[MethodForTable] = []
        seen: set[str] = set()
        for m, o, h in candidates:
            result.append(MethodForTable(method_id=m, op=o, hop=h, source_method_id=m))
            seen.add(m)
        mb = mb0
        for m, o, h in candidates:
            queue: list[tuple[str, int]] = [(m, 0)]
            v: set[str] = set()
            while queue:
                cur, hop = queue.pop(0)
                if cur in v or hop > max_hops:
                    continue
                v.add(cur)
                is_m = _is_method_node(backend, cur) or (
                    mb is not None and _is_method_node(mb, cur)
                )
                if hop > 0 and cur not in seen and is_m:
                    result.append(
                        MethodForTable(method_id=cur, op=o, hop=hop, source_method_id=m)
                    )
                    seen.add(cur)
                if mb:
                    preds = merged_predecessors_for_walk(backend, mb, cur, pred_walk)
                else:
                    preds = safe_predecessors_for_walk(backend, cur, pred_walk)
                if not preds:
                    continue
                for pred in preds:
                    if pred in v:
                        continue
                    queue.append((pred, hop + 1))
        return result
