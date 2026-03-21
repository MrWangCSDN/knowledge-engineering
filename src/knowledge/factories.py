"""知识层工厂：向量库与图后端，按 backend 字符串创建实例；支持 Registry 扩展（与 LLM 工厂对齐）。"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from src.core.weaviate_defaults import (
    DEFAULT_COLLECTION_CODE_ENTITY,
    DEFAULT_WEAVIATE_GRPC_PORT,
    DEFAULT_WEAVIATE_HTTP_URL,
)
from src.knowledge.abstractions import GraphBackendProtocol, VectorStoreProtocol

_LOG = logging.getLogger(__name__)

# (dimension, allow_fallback_to_memory, kwargs) -> 具体向量库实现
VectorStoreBuilder = Callable[[int, bool, dict[str, Any]], VectorStoreProtocol]

# kwargs 为 GraphBackendFactory.create 传入的 **kwargs
GraphBackendBuilder = Callable[[dict[str, Any]], GraphBackendProtocol]

_VECTOR_STORE_BUILDERS: dict[str, VectorStoreBuilder] = {}
_GRAPH_BACKEND_BUILDERS: dict[str, GraphBackendBuilder] = {}


def register_vector_store_backend(name: str, builder: VectorStoreBuilder) -> None:
    """注册向量库后端，与配置 ``backend`` 字符串对齐（小写）。"""
    key = (name or "").strip().lower()
    if not key:
        raise ValueError("register_vector_store_backend: name 不能为空")
    _VECTOR_STORE_BUILDERS[key] = builder


def unregister_vector_store_backend(name: str) -> None:
    _VECTOR_STORE_BUILDERS.pop((name or "").strip().lower(), None)


def registered_vector_store_backend_names() -> tuple[str, ...]:
    return tuple(sorted(_VECTOR_STORE_BUILDERS.keys()))


def register_graph_backend(name: str, builder: GraphBackendBuilder) -> None:
    """注册图后端，与配置 ``graph.backend`` 对齐（小写）。"""
    key = (name or "").strip().lower()
    if not key:
        raise ValueError("register_graph_backend: name 不能为空")
    _GRAPH_BACKEND_BUILDERS[key] = builder


def unregister_graph_backend(name: str) -> None:
    _GRAPH_BACKEND_BUILDERS.pop((name or "").strip().lower(), None)


def registered_graph_backend_names() -> tuple[str, ...]:
    return tuple(sorted(_GRAPH_BACKEND_BUILDERS.keys()))


def _vector_build_weaviate(dimension: int, allow_fallback_to_memory: bool, kwargs: dict[str, Any]) -> VectorStoreProtocol:
    try:
        from src.knowledge.vector_store_weaviate import WeaviateVectorStore

        return WeaviateVectorStore(
            url=kwargs.get("weaviate_url") or DEFAULT_WEAVIATE_HTTP_URL,
            grpc_port=int(kwargs.get("weaviate_grpc_port") or DEFAULT_WEAVIATE_GRPC_PORT),
            collection_name=kwargs.get("collection_name") or DEFAULT_COLLECTION_CODE_ENTITY,
            dimension=dimension,
            api_key=kwargs.get("weaviate_api_key"),
        )
    except Exception:
        if allow_fallback_to_memory:
            _LOG.warning(
                "Weaviate 向量库创建失败，已按配置回退内存 VectorStore",
                exc_info=True,
            )
            from src.knowledge.vector_store import VectorStore

            return VectorStore(dimension=dimension)
        raise


def _vector_build_memory(dimension: int, _allow_fallback_to_memory: bool, _kwargs: dict[str, Any]) -> VectorStoreProtocol:
    from src.knowledge.vector_store import VectorStore

    return VectorStore(dimension=dimension)


def _graph_build_neo4j(kwargs: dict[str, Any]) -> GraphBackendProtocol:
    from src.knowledge.graph_neo4j import Neo4jGraphBackend

    uri = kwargs.get("neo4j_uri") or "bolt://localhost:7687"
    user = kwargs.get("neo4j_user") or "neo4j"
    password = kwargs.get("neo4j_password") or "password"
    database = kwargs.get("neo4j_database") or "neo4j"
    return Neo4jGraphBackend(uri, user, password, database)


def _graph_build_memory(_kwargs: dict[str, Any]) -> GraphBackendProtocol:
    from src.knowledge.backends import MemoryGraphBackend

    return MemoryGraphBackend()


def _install_default_vector_backends() -> None:
    if _VECTOR_STORE_BUILDERS:
        return
    register_vector_store_backend("weaviate", _vector_build_weaviate)
    register_vector_store_backend("memory", _vector_build_memory)


def _install_default_graph_backends() -> None:
    if _GRAPH_BACKEND_BUILDERS:
        return
    register_graph_backend("neo4j", _graph_build_neo4j)
    register_graph_backend("memory", _graph_build_memory)


_install_default_vector_backends()
_install_default_graph_backends()


class VectorStoreFactory:
    """向量库工厂：按 registry 解析 backend；未知名回退 ``memory``。"""

    @staticmethod
    def create(
        backend: str,
        enabled: bool,
        dimension: int,
        *,
        allow_fallback_to_memory: bool = False,
        **kwargs: Any,
    ) -> Optional[VectorStoreProtocol]:
        """
        创建向量库实例。
        enabled: 若 False 返回 None
        dimension: 向量维度
        allow_fallback_to_memory: Weaviate 初始化失败时是否回退内存实现（默认否，直接抛错）
        kwargs: weaviate_url, weaviate_grpc_port, collection_name, weaviate_api_key 等
        """
        if not enabled:
            return None
        _install_default_vector_backends()
        key = (backend or "memory").strip().lower()
        builder = _VECTOR_STORE_BUILDERS.get(key)
        kw = dict(kwargs)
        if builder is not None:
            return builder(dimension, allow_fallback_to_memory, kw)
        return _vector_build_memory(dimension, allow_fallback_to_memory, kw)


class GraphBackendFactory:
    """图后端工厂：按 registry 解析 backend；未知名回退内存图。"""

    @staticmethod
    def create(backend: str, **kwargs: Any) -> GraphBackendProtocol:
        """
        创建图后端实例。
        kwargs (neo4j): neo4j_uri, neo4j_user, neo4j_password, neo4j_database
        """
        _install_default_graph_backends()
        key = (backend or "memory").strip().lower()
        builder = _GRAPH_BACKEND_BUILDERS.get(key)
        kw = dict(kwargs)
        if builder is not None:
            return builder(kw)
        return _graph_build_memory(kw)
