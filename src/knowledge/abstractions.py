"""知识层抽象接口：向量库与图后端及可选能力。"""
from __future__ import annotations

from typing import Any, Optional, Protocol, Sequence, runtime_checkable


@runtime_checkable
class ImpactClosureCapable(Protocol):
    """可选能力：提供有界影响闭包（沿全部边类型 up/down/both）。"""

    def impact_closure(
        self,
        start_id: str,
        *,
        direction: str = "down",
        max_depth: int = 50,
    ) -> set[str] | list[str]:
        ...


@runtime_checkable
class TraversalWithExclusionsCapable(Protocol):
    """可选能力：按排除关系类型获取后继/前驱（避免 implements 等扩散）。"""

    def successors_excluding_rel_types(
        self, nid: str, exclude_rel_types: Sequence[str]
    ) -> list[str]:
        ...

    def predecessors_excluding_rel_types(
        self, nid: str, exclude_rel_types: Sequence[str]
    ) -> list[str]:
        ...


@runtime_checkable
class VectorStoreProtocol(Protocol):
    """向量库抽象接口：存储实体嵌入，支持语义检索。"""

    def add(self, entity_id: str, vector: list[float], **kwargs: Any) -> None:
        """添加单条向量。"""
        ...

    def add_many(self, items: list[tuple[str, list[float]]]) -> None:
        """批量添加向量。"""
        ...

    def size(self) -> int:
        """返回向量数量。"""
        ...

    def search_by_vector(
        self, query_vector: list[float], top_k: int = 10
    ) -> list[tuple[str, float]]:
        """按向量相似度检索，返回 (entity_id, score) 列表。"""
        ...

    def search_by_text(
        self, query_text: str, top_k: int = 10
    ) -> list[tuple[str, float]]:
        """按文本语义检索。"""
        ...

    def get_by_entity_id(self, entity_id: str) -> Optional[dict[str, Any]]:
        """按 entity_id 取元数据（如 code_snippet）。"""
        ...

    def clear(self) -> None:
        """清空向量库。"""
        ...


@runtime_checkable
class GraphBackendProtocol(Protocol):
    """图后端抽象接口：节点与边的增删查、遍历。"""

    def add_node(self, nid: str, **attrs: Any) -> None:
        """添加节点。"""
        ...

    def add_edge(
        self, source_id: str, target_id: str, rel_type: str, **attrs: Any
    ) -> None:
        """添加边。"""
        ...

    def has_node(self, nid: str) -> bool:
        """判断节点是否存在。"""
        ...

    def get_node(self, nid: str) -> Optional[dict]:
        """获取节点属性。"""
        ...

    def successors(self, nid: str, rel_type: Optional[str] = None) -> list[str]:
        """获取后继节点 ID 列表。"""
        ...

    def successors_excluding_rel_types(
        self, nid: str, exclude_rel_types: Sequence[str]
    ) -> list[str]:
        """获取后继节点 ID；不沿 rel_type（及 Neo4j 关系类型）属于 exclude 的出边。exclude 为小写名即可，如 implements。"""
        ...

    def predecessors(self, nid: str, rel_type: Optional[str] = None) -> list[str]:
        """获取前驱节点 ID 列表。"""
        ...

    def predecessors_excluding_rel_types(
        self, nid: str, exclude_rel_types: Sequence[str]
    ) -> list[str]:
        """获取前驱节点 ID；不沿 rel_type（及 Neo4j 关系类型）属于 exclude 的入边。exclude 为小写名即可，如 implements。"""
        ...

    def node_count(self) -> int:
        """节点总数。"""
        ...

    def edge_count(self) -> int:
        """边总数。"""
        ...

    def clear(self) -> None:
        """清空图。"""
        ...

    def close(self) -> None:
        """释放连接等资源。"""
        ...
