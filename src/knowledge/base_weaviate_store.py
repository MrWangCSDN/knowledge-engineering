"""Weaviate Store 基类：统一连接、建表、清理与关闭生命周期。"""
from __future__ import annotations

import hashlib
import inspect
from abc import ABC, abstractmethod
from typing import Any, Optional


class BaseWeaviateStore(ABC):
    """抽象基类：子类只需提供 schema 属性清单。"""

    def __init__(
        self,
        *,
        url: str,
        grpc_port: int,
        collection_name: str,
        dimension: int,
        api_key: Optional[str] = None,
    ):
        self._url = url
        self._grpc_port = grpc_port
        self._collection_name = collection_name
        self._dim = dimension
        self._api_key = api_key
        self._client = None
        self._ensure_client_and_schema()

    @staticmethod
    def _to_uuid(s: str) -> str:
        h = hashlib.sha256(s.encode("utf-8")).hexdigest()[:32]
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"

    @staticmethod
    def _parse_url(url: str) -> tuple[str, int, bool]:
        secure = url.startswith("https://")
        rest = url.replace("https://", "").replace("http://", "").strip("/")
        if ":" in rest:
            host, port_s = rest.rsplit(":", 1)
            return host, int(port_s), secure
        return rest, 443 if secure else 8080, secure

    @abstractmethod
    def _schema_properties(self) -> list[Any]:
        """返回 collection 属性定义列表（weaviate.classes.config.Property[]）。"""
        raise NotImplementedError

    def _ensure_client_and_schema(self) -> None:
        import os
        import weaviate
        from weaviate.classes.config import Configure

        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

        # 清除代理环境变量，防止 gRPC/HTTP 连接被系统代理拦截
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            os.environ.pop(k, None)
        os.environ["NO_PROXY"] = os.environ.get("NO_PROXY", "") + ",localhost,127.0.0.1"

        host, port, secure = self._parse_url(self._url)
        conn_kw: dict[str, Any] = dict(
            http_host=host,
            http_port=port,
            http_secure=secure,
            grpc_host=host,
            grpc_port=self._grpc_port,
            grpc_secure=secure,
        )
        if self._api_key:
            try:
                from weaviate.auth import Auth

                conn_kw["auth_credentials"] = Auth.api_key(self._api_key)
            except Exception:
                pass

        # v2.0 staging：跳过 gRPC ping init check（启动期不稳定，操作时再用）
        conn_kw["skip_init_checks"] = True
        client = weaviate.connect_to_custom(**conn_kw)
        try:
            if not client.collections.exists(self._collection_name):
                props = self._schema_properties()
                try:
                    from weaviate.classes.config import VectorDistances

                    vec_index = Configure.VectorIndex.hnsw(distance_metric=VectorDistances.COSINE)
                except Exception:
                    vec_index = Configure.VectorIndex.hnsw(distance_metric="cosine")  # type: ignore[arg-type]
                # v2.0：所有 collection 默认启用 Multi-Tenancy（MT），并允许自动创建/激活 tenant
                mt_config = Configure.multi_tenancy(
                    enabled=True,
                    auto_tenant_creation=True,
                    auto_tenant_activation=True,
                )
                try:
                    # v4.10+: Configure.Vectors; v4.4-4.9: vectorizer_config=None + vector_index_config
                    if hasattr(Configure, "Vectors"):
                        client.collections.create(
                            name=self._collection_name,
                            vector_config=Configure.Vectors.self_provided(vector_index_config=vec_index),
                            properties=props,
                            multi_tenancy_config=mt_config,
                        )
                    else:
                        client.collections.create(
                            name=self._collection_name,
                            vectorizer_config=Configure.Vectorizer.none(),
                            vector_index_config=vec_index,
                            properties=props,
                            multi_tenancy_config=mt_config,
                        )
                except TypeError:
                    params = inspect.signature(Configure.VectorIndex.hnsw).parameters
                    kwargs: dict[str, Any] = {}
                    try:
                        from weaviate.classes.config import VectorDistances as VD

                        kwargs["distance_metric"] = VD.COSINE
                    except Exception:
                        kwargs["distance_metric"] = "cosine"
                    if "vector_size" in params:
                        kwargs["vector_size"] = self._dim
                    elif "dimensions" in params:
                        kwargs["dimensions"] = self._dim
                    try:
                        vec_index_cfg = Configure.VectorIndex.hnsw(**kwargs)
                    except TypeError:
                        kwargs["distance_metric"] = "cosine"
                        vec_index_cfg = Configure.VectorIndex.hnsw(**kwargs)
                    client.collections.create(
                        name=self._collection_name,
                        vectorizer_config=Configure.Vectorizer.none(),
                        vector_index_config=vec_index_cfg,
                        properties=props,
                        multi_tenancy_config=mt_config,
                    )
            self._client = client
        except Exception:
            try:
                client.close()
            except Exception:
                pass
            raise

    def _get_collection(self):
        return self._client.collections.get(self._collection_name)

    def clear(self, tenant: Optional[str] = None) -> None:
        # 多工程隔离（Phase1-T2）：tenant 为真值时只清该租户，否则保持旧的全集合删。
        # tenant 真值分支：用 v4 Multi-Tenancy API 移除单个租户的数据，
        # 绝不删整个集合（删整集合会清掉所有工程的代码向量 → 多工程互毁）。
        if tenant:
            try:
                coll = self._get_collection()
                # tenants.remove 接受 Sequence[str]；建表时 auto_tenant_creation=True，
                # 下次 with_tenant(tenant) 写入会自动重建该租户，故无需重建 schema。
                # remove 对不存在的租户可能抛异常，整段 best-effort 吞掉。
                coll.tenants.remove([tenant])
            except Exception:
                pass
            return
        # tenant 为 None / 空：保持现状全集合删 + 重建 schema（向后兼容单租户 / legacy / 现有测试）。
        try:
            if self._client and self._client.collections.exists(self._collection_name):
                self._client.collections.delete(self._collection_name)
            self._ensure_client_and_schema()
        except Exception:
            pass

    def close(self) -> None:
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

