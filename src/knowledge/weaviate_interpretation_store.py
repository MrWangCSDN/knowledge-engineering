"""方法技术解读：独立 Weaviate collection，与图谱 method 节点通过 method_entity_id 关联；存文本 + 向量。"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from src.core.weaviate_defaults import (
    DEFAULT_COLLECTION_METHOD_INTERPRETATION,
    DEFAULT_WEAVIATE_GRPC_PORT,
    DEFAULT_WEAVIATE_HTTP_URL,
)
from src.knowledge.base_weaviate_store import BaseWeaviateStore
from src.knowledge.method_entity_id_normalize import method_entity_id_variants


class WeaviateMethodInterpretStore(BaseWeaviateStore):
    """collection：MethodInterpretation 等；method_entity_id ↔ 图谱方法节点。"""

    def __init__(
        self,
        url: str = DEFAULT_WEAVIATE_HTTP_URL,
        grpc_port: int = DEFAULT_WEAVIATE_GRPC_PORT,
        collection_name: str = DEFAULT_COLLECTION_METHOD_INTERPRETATION,
        dimension: int = 64,
        api_key: Optional[str] = None,
    ):
        super().__init__(
            url=url,
            grpc_port=grpc_port,
            collection_name=collection_name,
            dimension=dimension,
            api_key=api_key,
        )

    def _schema_properties(self) -> list[Any]:
        from weaviate.classes.config import Configure, Property, DataType
        _ = Configure
        return [
            Property(name="method_entity_id", data_type=DataType.TEXT),
            Property(name="class_entity_id", data_type=DataType.TEXT),
            Property(name="class_name", data_type=DataType.TEXT),
            Property(name="method_name", data_type=DataType.TEXT),
            Property(name="signature", data_type=DataType.TEXT),
            Property(name="interpretation_text", data_type=DataType.TEXT),
            Property(name="context_summary", data_type=DataType.TEXT),
            Property(name="language", data_type=DataType.TEXT),
            Property(name="related_entity_ids_json", data_type=DataType.TEXT),
        ]

    def add(
        self,
        vector: list[float],
        method_entity_id: str,
        interpretation_text: str,
        *,
        class_entity_id: str = "",
        class_name: str = "",
        method_name: str = "",
        signature: str = "",
        context_summary: str = "",
        language: str = "zh",
        related_entity_ids_json: str = "{}",
    ) -> bool:
        """写入一条解读，成功返回 True，失败返回 False。已存在则 upsert 覆盖。"""
        ok, _created = self.add_with_created(
            vector=vector,
            method_entity_id=method_entity_id,
            interpretation_text=interpretation_text,
            class_entity_id=class_entity_id,
            class_name=class_name,
            method_name=method_name,
            signature=signature,
            context_summary=context_summary,
            language=language,
            related_entity_ids_json=related_entity_ids_json,
        )
        return ok

    def add_with_created(
        self,
        vector: list[float],
        method_entity_id: str,
        interpretation_text: str,
        *,
        class_entity_id: str = "",
        class_name: str = "",
        method_name: str = "",
        signature: str = "",
        context_summary: str = "",
        language: str = "zh",
        related_entity_ids_json: str = "{}",
    ) -> tuple[bool, bool]:
        """
        写入一条解读并返回 (成功, 是否新建)。
        - 成功：insert 或 replace 没报错
        - 是否新建：仅当首次 insert 创建新对象时为 True；若已存在则 replace 为 False
        """
        if not vector or len(vector) < self._dim:
            return False, False
        coll = self._get_collection()
        uid = self._to_uuid(method_entity_id + "|interpret")
        props = {
            "method_entity_id": method_entity_id,
            "class_entity_id": class_entity_id or "",
            "class_name": (class_name or "")[:500],
            "method_name": (method_name or "")[:300],
            "signature": (signature or "")[:2000],
            "interpretation_text": (interpretation_text or "")[:48000],
            "context_summary": (context_summary or "")[:12000],
            "language": language or "zh",
            "related_entity_ids_json": related_entity_ids_json[:8000],
        }
        vec = vector[: self._dim]
        try:
            coll.data.insert(properties=props, vector=vec, uuid=uid)
            return True, True
        except Exception as e:
            if "already exists" in str(e).lower() or "422" in str(e):
                try:
                    coll.data.replace(uuid=uid, properties=props, vector=vec)
                    return True, False
                except Exception as e2:
                    logging.getLogger(__name__).warning(
                        "Weaviate 技术解读 replace 失败 (method=%s): %s",
                        method_entity_id[:50] if method_entity_id else "?",
                        e2,
                    )
                    return False, False
            logging.getLogger(__name__).warning(
                "Weaviate 技术解读写入失败 (method=%s): %s",
                method_entity_id[:50] if method_entity_id else "?",
                e,
            )
            return False, False

    def get_by_method_id(self, method_entity_id: str) -> Optional[dict[str, Any]]:
        if not method_entity_id or not self._client:
            return None
        try:
            from weaviate.classes.query import Filter

            coll = self._get_collection()
            for mid_try in method_entity_id_variants(method_entity_id):
                result = coll.query.fetch_objects(
                    filters=Filter.by_property("method_entity_id").equal(mid_try),
                    limit=1,
                )
                for obj in result.objects:
                    p = obj.properties or {}
                    return {
                        "method_entity_id": p.get("method_entity_id", mid_try),
                        "method_name": p.get("method_name", ""),
                        "signature": p.get("signature", ""),
                        "interpretation_text": p.get("interpretation_text", ""),
                        "class_entity_id": p.get("class_entity_id", ""),
                        "class_name": p.get("class_name", ""),
                        "language": p.get("language", ""),
                        "context_summary": p.get("context_summary", ""),
                        "related_entity_ids_json": p.get("related_entity_ids_json", ""),
                    }
            return None
        except Exception:
            return None

    def count(self) -> int:
        """返回 collection 中的对象总数，用于显示真实解读进度。"""
        if not self._client:
            return 0
        try:
            coll = self._get_collection()
            return coll.aggregate.over_all(total_count=True).total_count
        except Exception:
            # fallback：避免某些 SDK 计数不可用导致 UI 永久为 0
            try:
                return len(self.list_existing_method_ids(limit=200000))
            except Exception:
                return 0

    def list_existing_method_ids(self, limit: int = 100000) -> set[str]:
        """列出当前 collection 中已有的 method_entity_id 集合，用于断点续跑时跳过已完成的方法。"""
        ids: set[str] = set()
        if not self._client:
            return ids
        try:
            coll = self._get_collection()
            # 分页抓取：避免一次性 limit=100000 导致 SDK/服务端压力过大或抛异常。
            page_size = 2000
            fetched = 0
            target = max(0, int(limit))
            while fetched < target:
                cur_limit = min(page_size, target - fetched)
                try:
                    result = coll.query.fetch_objects(
                        limit=cur_limit,
                        offset=fetched,
                        return_properties=["method_entity_id"],
                    )
                except TypeError:
                    # offset 或 return_properties 可能不被当前 SDK 支持：退化为单页拉取一次
                    try:
                        result = coll.query.fetch_objects(limit=target, return_properties=["method_entity_id"])
                    except TypeError:
                        result = coll.query.fetch_objects(limit=target)
                    for obj in (result.objects or []):
                        p = obj.properties or {}
                        mid = p.get("method_entity_id")
                        if isinstance(mid, str) and mid:
                            ids.add(mid)
                    break

                objs = result.objects or []
                if not objs:
                    break
                for obj in objs:
                    p = obj.properties or {}
                    mid = p.get("method_entity_id")
                    if isinstance(mid, str) and mid:
                        ids.add(mid)
                fetched += len(objs)
                if len(objs) < cur_limit:
                    break
        except Exception:
            return ids
        return ids

    def search_by_text(self, query_text: str, top_k: int = 10) -> list[tuple[str, float]]:
        """按技术解读文本的向量做相似检索，返回 (method_entity_id, score)，score 越大越相似。"""
        from src.semantic.embedding import get_embedding
        from src.knowledge.weaviate_near_vector import near_vector_property_hits

        if not (query_text or "").strip() or not self._client:
            return []
        vec = get_embedding(query_text.strip(), self._dim)
        coll = self._get_collection()
        rows = near_vector_property_hits(
            coll,
            vector=vec,
            dim=self._dim,
            limit=int(top_k),
            collection_name=self._collection_name,
            return_properties=[
                "method_entity_id",
                "method_name",
                "interpretation_text",
                "signature",
            ],
        )
        out: list[tuple[str, float]] = []
        for props, score in rows:
            mid = str(props.get("method_entity_id") or "").strip()
            if mid:
                out.append((mid, float(score)))
        return out

    # clear/close/__del__ 由 BaseWeaviateStore 提供
