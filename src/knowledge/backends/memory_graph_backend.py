"""内存图后端：基于 NetworkX，与 Neo4jGraphBackend 接口一致。"""
from __future__ import annotations

from typing import Any, Optional, Sequence, Set

import networkx as nx

from src.knowledge.abstractions import GraphBackendProtocol


class MemoryGraphBackend:
    """基于 NetworkX 的内存图后端，提供与 Neo4j 一致的接口。"""

    def __init__(self) -> None:
        self._g: nx.MultiDiGraph = nx.MultiDiGraph()

    def add_node(self, nid: str, **attrs: Any) -> None:
        self._g.add_node(nid, **{k: v for k, v in attrs.items() if v is not None})

    def add_edge(
        self, source_id: str, target_id: str, rel_type: str, **attrs: Any
    ) -> None:
        self._g.add_edge(source_id, target_id, rel_type=rel_type, **attrs)

    def has_node(self, nid: str) -> bool:
        return self._g.has_node(nid)

    def get_node(self, nid: str) -> Optional[dict]:
        if not self._g.has_node(nid):
            return None
        data = dict(self._g.nodes[nid])
        data["id"] = nid
        return data

    @staticmethod
    def _rel_matches(edge_rel: Any, want: Optional[str]) -> bool:
        if want is None:
            return True
        return str(edge_rel or "").lower() == str(want).lower()

    def successors(self, nid: str, rel_type: Optional[str] = None) -> list[str]:
        if not self._g.has_node(nid):
            return []
        out = []
        for _, target, k in self._g.out_edges(nid, keys=True):
            ed = self._g.edges[nid, target, k]
            if self._rel_matches(ed.get("rel_type"), rel_type):
                out.append(target)
        return out

    def successors_excluding_rel_types(
        self, nid: str, exclude_rel_types: Sequence[str]
    ) -> list[str]:
        if not self._g.has_node(nid):
            return []
        exc = {str(x).strip().lower() for x in exclude_rel_types if str(x).strip()}
        if not exc:
            return self.successors(nid, rel_type=None)
        out: list[str] = []
        for _, target, k in self._g.out_edges(nid, keys=True):
            ed = self._g.edges[nid, target, k]
            rt = str(ed.get("rel_type") or "").strip().lower()
            if rt in exc:
                continue
            out.append(target)
        return out

    def predecessors(self, nid: str, rel_type: Optional[str] = None) -> list[str]:
        if not self._g.has_node(nid):
            return []
        out = []
        for src, _, k in self._g.in_edges(nid, keys=True):
            ed = self._g.edges[src, nid, k]
            if self._rel_matches(ed.get("rel_type"), rel_type):
                out.append(src)
        return out

    def predecessors_excluding_rel_types(
        self, nid: str, exclude_rel_types: Sequence[str]
    ) -> list[str]:
        if not self._g.has_node(nid):
            return []
        exc = {str(x).strip().lower() for x in exclude_rel_types if str(x).strip()}
        if not exc:
            return self.predecessors(nid, rel_type=None)
        out: list[str] = []
        for src, _, k in self._g.in_edges(nid, keys=True):
            ed = self._g.edges[src, nid, k]
            rt = str(ed.get("rel_type") or "").strip().lower()
            if rt in exc:
                continue
            out.append(src)
        return out

    def node_count(self) -> int:
        return self._g.number_of_nodes()

    def edge_count(self) -> int:
        return self._g.number_of_edges()

    def impact_closure(
        self, start_id: str, direction: str = "down", max_depth: int = 50
    ) -> Set[str]:
        """沿**全部类型**出边/入边做有界遍历（与 Neo4jGraphBackend 一致），含 calls、implements、belongs_to 等。"""
        seen: set[str] = set()
        stack = [start_id]
        depth = 0
        while stack and depth < max_depth:
            depth += 1
            nid = stack.pop()
            if nid in seen:
                continue
            seen.add(nid)
            next_ids = self.successors(nid, rel_type=None) if direction == "down" else self.predecessors(
                nid, rel_type=None
            )
            for k in next_ids:
                if k not in seen:
                    stack.append(k)
        return seen

    def clear(self) -> None:
        self._g.clear()

    def close(self) -> None:
        """内存图无需释放连接。"""
        pass
