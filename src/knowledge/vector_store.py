"""知识层向量库：存储代码实体嵌入，支持语义相似检索。"""
from __future__ import annotations

from typing import Any, Optional

from src.semantic.embedding import cosine_similarity, get_embedding


class VectorStore:
    """内存向量库：entity_id -> vector；支持按 query 向量检索 top_k 最相似实体。"""

    def __init__(self, dimension: int = 64):
        self._dim = dimension
        self._ids: list[str] = []
        self._vectors: list[list[float]] = []

    def add(self, entity_id: str, vector: list[float], **kwargs: object) -> None:
        if not vector or len(vector) != self._dim:
            return
        self._ids.append(entity_id)
        self._vectors.append(vector[: self._dim])

    def add_many(self, items: list[tuple[str, list[float]]]) -> None:
        for eid, vec in items:
            if vec and len(vec) >= self._dim:
                self._ids.append(eid)
                self._vectors.append(vec[: self._dim])

    def size(self) -> int:
        return len(self._ids)

    def search_by_vector(self, query_vector: list[float], top_k: int = 10) -> list[tuple[str, float]]:
        """按向量相似度返回 (entity_id, score) 列表。"""
        if not query_vector or not self._vectors:
            return []
        scores = [cosine_similarity(query_vector, v) for v in self._vectors]
        indexed = list(zip(self._ids, scores))
        indexed.sort(key=lambda x: -x[1])
        return indexed[:top_k]

    def search_by_text(self, query_text: str, top_k: int = 10) -> list[tuple[str, float]]:
        """将 query 文本编码为向量后检索。"""
        vec = get_embedding(query_text, self._dim)
        return self.search_by_vector(vec, top_k)

    def get_by_entity_id(self, entity_id: str) -> Optional[dict[str, Any]]:
        """内存向量库未存 code_snippet，返回 None；接口与 Weaviate 一致便于「方法→代码」双向关联。"""
        return None

    def clear(self) -> None:
        self._ids = []
        self._vectors = []

    def close(self) -> None:
        """内存向量库无需释放连接，接口兼容。"""
        pass
