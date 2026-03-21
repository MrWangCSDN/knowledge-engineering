"""影响分析：闭包与列表构建等纯逻辑（无 Streamlit）。"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable

from src.knowledge.abstractions import ImpactClosureCapable


@dataclass(frozen=True, slots=True)
class ImpactNodeRow:
    nid: str
    entity_type: str
    label: str


def compute_impact_closure_set(
    backend: Any,
    start: str,
    *,
    mode: str,
    max_depth: int,
) -> set[str]:
    if isinstance(backend, ImpactClosureCapable):
        depth = int(max_depth)
        if mode == "down":
            raw = backend.impact_closure(start, direction="down", max_depth=depth)
            return set(raw)
        if mode == "up":
            raw = backend.impact_closure(start, direction="up", max_depth=depth)
            return set(raw)
        down = set(backend.impact_closure(start, direction="down", max_depth=depth))
        up = set(backend.impact_closure(start, direction="up", max_depth=depth))
        return down | up
    return {start}


def build_impact_node_rows(
    closure: set[str],
    get_node: Callable[[str], dict[str, Any] | None],
) -> list[ImpactNodeRow]:
    rows: list[ImpactNodeRow] = []
    for nid in closure:
        node = get_node(nid) or {}
        et = str(node.get("entity_type") or "").lower()
        label = str(node.get("name") or nid)
        rows.append(ImpactNodeRow(nid=nid, entity_type=et, label=label))
    return rows


def impact_type_histogram_top(rows: list[ImpactNodeRow], top_k: int = 8) -> list[tuple[str, int]]:
    c: Counter[str] = Counter()
    for r in rows:
        c[r.entity_type or "unknown"] += 1
    return c.most_common(top_k)


def sorted_impact_node_rows(rows: list[ImpactNodeRow]) -> list[ImpactNodeRow]:
    return sorted(rows, key=lambda r: (r.entity_type, r.label))


def take_top_n(rows: list[ImpactNodeRow], n: int) -> list[ImpactNodeRow]:
    return rows[: max(0, int(n))]
