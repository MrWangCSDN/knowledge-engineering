"""方法↔表图遍历：BFS 与后继/前驱策略（与 MethodTableAccessService 编排分离）。"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

from src.knowledge.abstractions import TraversalWithExclusionsCapable
from src.knowledge.method_entity_id_normalize import method_entity_id_variants


@dataclass(frozen=True, slots=True)
class GraphWalkSuccessorConfig:
    """
    方法查表 BFS（出边）策略：calls_only 时仅沿 calls；否则沿全类型出边但排除指定关系，
    并丢弃指向指定 id 前缀的目标节点。
    """

    calls_only: bool
    excluded_edge_rel_types: tuple[str, ...]
    excluded_target_id_prefixes: tuple[str, ...]

    @staticmethod
    def method_to_table_default() -> GraphWalkSuccessorConfig:
        return GraphWalkSuccessorConfig(
            calls_only=False,
            excluded_edge_rel_types=("implements",),
            excluded_target_id_prefixes=("term://", "domain://", "capability://"),
        )

    @staticmethod
    def calls_only_default() -> GraphWalkSuccessorConfig:
        return GraphWalkSuccessorConfig(
            calls_only=True,
            excluded_edge_rel_types=(),
            excluded_target_id_prefixes=(),
        )


@dataclass(frozen=True, slots=True)
class GraphWalkPredecessorConfig:
    """表查方法反向 BFS（入边）策略。"""

    excluded_edge_rel_types: tuple[str, ...]
    excluded_target_id_prefixes: tuple[str, ...]

    @staticmethod
    def table_to_method_default() -> GraphWalkPredecessorConfig:
        return GraphWalkPredecessorConfig(
            excluded_edge_rel_types=("implements",),
            excluded_target_id_prefixes=("term://", "domain://", "capability://"),
        )


def filter_ids_excluding_prefixes(ids: list[str], prefixes: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for x in ids:
        if not x:
            continue
        s = str(x).strip().lower()
        if any(s.startswith(p) for p in prefixes):
            continue
        out.append(x)
    return out


def safe_predecessors_for_walk(
    backend: Any, mid: str, walk: GraphWalkPredecessorConfig
) -> list[str]:
    if not backend or not mid:
        return []
    try:
        if isinstance(backend, TraversalWithExclusionsCapable):
            raw = backend.predecessors_excluding_rel_types(
                mid, walk.excluded_edge_rel_types
            ) or []
        else:
            raw = backend.predecessors(mid, rel_type=None) or []
    except Exception:
        return []
    raw = list(dict.fromkeys([x for x in raw if x]))
    return filter_ids_excluding_prefixes(raw, walk.excluded_target_id_prefixes)


def merged_predecessors_for_walk(
    primary: Any, secondary: Any | None, mid: str, walk: GraphWalkPredecessorConfig
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for b in (primary, secondary):
        if b is None:
            continue
        for x in safe_predecessors_for_walk(b, mid, walk):
            if x not in seen:
                seen.add(x)
                out.append(x)
    return out


def safe_successors_for_walk(
    backend: Any, mid: str, walk: GraphWalkSuccessorConfig
) -> list[str]:
    if not backend or not mid:
        return []
    try:
        if walk.calls_only:
            raw = backend.successors(mid, rel_type="calls") or []
        elif isinstance(backend, TraversalWithExclusionsCapable):
            raw = backend.successors_excluding_rel_types(
                mid, walk.excluded_edge_rel_types
            ) or []
        else:
            raw = backend.successors(mid, rel_type=None) or []
    except Exception:
        return []
    raw = list(dict.fromkeys([x for x in raw if x]))
    if walk.calls_only:
        return raw
    return filter_ids_excluding_prefixes(raw, walk.excluded_target_id_prefixes)


def merged_successors_for_walk(
    primary: Any, secondary: Any | None, mid: str, walk: GraphWalkSuccessorConfig
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for b in (primary, secondary):
        if b is None:
            continue
        for x in safe_successors_for_walk(b, mid, walk):
            if x not in seen:
                seen.add(x)
                out.append(x)
    return out


def bfs_min_hops(
    start: str,
    backend: Any,
    max_hops: int,
    merge_backend: Any | None = None,
    *,
    successor_walk: GraphWalkSuccessorConfig | None = None,
) -> tuple[dict[str, int], dict[str, str | None]]:
    walk = successor_walk or GraphWalkSuccessorConfig.method_to_table_default()
    best: dict[str, int] = {start: 0}
    parent: dict[str, str | None] = {start: None}
    q: deque[str] = deque([start])
    secondary = merge_backend if merge_backend is not backend else None
    while q:
        mid = q.popleft()
        h = best[mid]
        if h >= max_hops:
            continue
        if secondary:
            succs = merged_successors_for_walk(backend, secondary, mid, walk)
        else:
            succs = safe_successors_for_walk(backend, mid, walk)
        for succ in succs:
            if not succ:
                continue
            nh = h + 1
            if nh > max_hops:
                continue
            if succ not in best:
                best[succ] = nh
                parent[succ] = mid
                q.append(succ)
    return best, parent


def _backend_has_node(backend: Any, nid: str) -> bool:
    if not backend or not nid:
        return False
    has_fn = getattr(backend, "has_node", None)
    if callable(has_fn):
        try:
            return bool(has_fn(nid))
        except Exception:
            pass
    get_fn = getattr(backend, "get_node", None)
    if callable(get_fn):
        try:
            return get_fn(nid) is not None
        except Exception:
            pass
    return False


def _canonical_method_id(backend: Any, method_id: str) -> str:
    if not method_id or not backend:
        return method_id
    variants = method_entity_id_variants(method_id) or [method_id]
    for v in variants:
        if _backend_has_node(backend, v):
            return v
    return method_id


def resolve_bfs_start_id(primary: Any, secondary: Any | None, method_id: str) -> str:
    c1 = _canonical_method_id(primary, method_id)
    if _backend_has_node(primary, c1):
        return c1
    if secondary is not None and secondary is not primary:
        c2 = _canonical_method_id(secondary, method_id)
        if _backend_has_node(secondary, c2):
            return c2
    return c1


def reconstruct_path(parent: dict[str, str | None], start: str, end: str) -> list[str]:
    if end == start:
        return [start]
    path_rev: list[str] = []
    cur: str | None = end
    for _ in range(500):
        if cur is None:
            break
        path_rev.append(cur)
        if cur == start:
            break
        cur = parent.get(cur)
    if not path_rev or path_rev[-1] != start:
        return [start, end]
    return list(reversed(path_rev))
