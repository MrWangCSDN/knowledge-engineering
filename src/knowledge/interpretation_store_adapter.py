from __future__ import annotations

from typing import Any, Optional, Protocol, TypeVar, Generic


K = TypeVar("K")


class InterpretationStoreAdapterProtocol(Protocol, Generic[K]):
    """解读存储适配器：断点续跑键、计数、写入、清理与释放。"""

    def list_existing_keys(self, limit: int = 200000) -> set[K]:
        ...

    def count(self) -> int:
        ...

    def add(self, *args: Any, **kwargs: Any) -> bool:
        """写入一条解读；具体参数由技术/业务 Weaviate store 定义。"""
        ...

    def clear(self) -> None:
        ...

    def close(self) -> None:
        ...


class MethodInterpretationStoreAdapter(InterpretationStoreAdapterProtocol[str]):
    """适配器：把 WeaviateMethodInterpretStore 统一成方法解读存储接口。"""

    def __init__(self, store: Any):
        self._store = store

    def list_existing_keys(self, limit: int = 100000) -> set[str]:
        return self._store.list_existing_method_ids(limit=limit)

    def count(self) -> int:
        return self._store.count()

    def clear(self) -> None:
        return self._store.clear()

    def close(self) -> None:
        return self._store.close()

    # 为保持对现有 runner 的改动最小，这里透传 add() 签名
    def add(self, *args: Any, **kwargs: Any) -> bool:
        return self._store.add(*args, **kwargs)


class BusinessInterpretationStoreAdapter(InterpretationStoreAdapterProtocol[tuple[str, str]]):
    """适配器：把 WeaviateBusinessInterpretStore 统一成业务解读存储接口。"""

    def __init__(self, store: Any):
        self._store = store

    def list_existing_keys(self, limit: int = 200000) -> set[tuple[str, str]]:
        return self._store.list_existing_entity_level_pairs(limit=limit)

    def count(self) -> int:
        return self._store.count()

    def clear(self) -> None:
        return self._store.clear()

    def close(self) -> None:
        return self._store.close()

    # 透传 add() 签名给业务 runner
    def add(self, *args: Any, **kwargs: Any) -> bool:
        return self._store.add(*args, **kwargs)

