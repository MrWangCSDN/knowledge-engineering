"""知识层向量库：Weaviate 后端，与 VectorStore 接口一致；支持方法代码片段与 entity_id 关联图谱节点。"""
from __future__ import annotations

from src.core.weaviate_defaults import (
    DEFAULT_COLLECTION_CODE_ENTITY,
    DEFAULT_WEAVIATE_GRPC_PORT,
    DEFAULT_WEAVIATE_HTTP_URL,
)

from typing import Any, Optional

from src.semantic.embedding import get_embedding
from src.knowledge.base_weaviate_store import BaseWeaviateStore
from src.knowledge.method_entity_id_normalize import method_entity_id_variants


class WeaviateVectorStore(BaseWeaviateStore):
    """使用 Weaviate 存储与检索代码实体向量；entity_id 与知识图谱中方法节点一一对应。"""

    def __init__(
        self,
        url: str = DEFAULT_WEAVIATE_HTTP_URL,
        grpc_port: int = DEFAULT_WEAVIATE_GRPC_PORT,
        collection_name: str = DEFAULT_COLLECTION_CODE_ENTITY,
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
        # 用于 UI 调试：记录最近一次向量检索异常（若无异常则为 None）
        self._last_search_error: str | None = None
        # 用于 UI 调试：记录最近一次向量检索的返回形态诊断（即便无异常也可能为空）
        self._last_search_detail: str | None = None

    def _schema_properties(self) -> list[Any]:
        from weaviate.classes.config import Configure, Property, DataType
        _ = Configure
        return [
            Property(name="entity_id", data_type=DataType.TEXT),
            Property(name="name", data_type=DataType.TEXT),
            Property(name="entity_type", data_type=DataType.TEXT),
            Property(name="code_snippet", data_type=DataType.TEXT),
        ]

    def _name_from_id(self, entity_id: str) -> str:
        return (entity_id.split("/")[-1] if "/" in entity_id else entity_id)[:100]

    def add(
        self,
        entity_id: str,
        vector: list[float],
        entity_type: Optional[str] = None,
        name: Optional[str] = None,
        code_snippet: Optional[str] = None,
    ) -> None:
        if not vector or len(vector) < self._dim:
            return
        try:
            coll = self._get_collection()
            vec = vector[: self._dim]
            props = {
                "entity_id": entity_id,
                "name": (name or self._name_from_id(entity_id))[:100],
                "entity_type": entity_type or "",
                "code_snippet": code_snippet or "",
            }
            coll.data.insert(
                properties=props,
                vector=vec,
                uuid=self._to_uuid(entity_id),
            )
        except Exception:
            pass

    def add_many(self, items: list[tuple[str, list[float]]]) -> None:
        try:
            coll = self._get_collection()
            with coll.batch.dynamic() as batch:
                for i, (eid, vec) in enumerate(items):
                    if not vec or len(vec) < self._dim:
                        continue
                    batch.add_object(
                        properties={
                            "entity_id": eid,
                            "name": self._name_from_id(eid),
                            "entity_type": "",
                            "code_snippet": "",
                        },
                        vector=vec[: self._dim],
                        uuid=self._to_uuid(eid + str(i)),
                    )
        except Exception:
            pass

    def size(self) -> int:
        try:
            coll = self._get_collection()
            return coll.aggregate.over_all(total_count=True).total_count
        except Exception:
            return 0

    def search_by_vector(self, query_vector: list[float], top_k: int = 10) -> list[tuple[str, float]]:
        self._last_search_error = None
        self._last_search_detail = None
        if not query_vector or len(query_vector) < self._dim:
            return []
        try:
            coll = self._get_collection()
            # 注意：在某些 weaviate-python-client 版本组合下，如果不显式指定 return_properties，
            # result.objects 可能会为空（即便集合里有数据）。因此这里强制指定返回的字段。
            from weaviate.classes.query import MetadataQuery

            metadata = MetadataQuery(distance=True)
            query = coll.query.near_vector(
                near_vector=query_vector[: self._dim],
                limit=top_k,
                return_properties=["entity_id", "name", "entity_type", "code_snippet"],
                return_metadata=metadata,
            )

            # 兼容：部分 SDK 会返回“可执行查询对象”，需要调用 do() 才会得到结果
            result = query.do() if hasattr(query, "do") else query

            def _extract_objects(maybe_result: Any) -> list[Any]:
                """不同 SDK 版本/返回模式下，对象列表可能出现在不同位置。"""
                if maybe_result is None:
                    return []
                objs_attr = getattr(maybe_result, "objects", None)
                if objs_attr is not None:
                    return list(objs_attr or [])
                if isinstance(maybe_result, list):
                    return maybe_result
                if isinstance(maybe_result, dict):
                    # 1) 直接 objects
                    if isinstance(maybe_result.get("objects"), list):
                        return maybe_result.get("objects") or []

                    # 2) GraphQL 风格：data -> Get -> <CollectionName> -> [objects]
                    data = maybe_result.get("data") or maybe_result.get("Data") or {}
                    if isinstance(data, dict):
                        get = data.get("Get") or data.get("get") or {}
                        if isinstance(get, dict):
                            col = self._collection_name
                            if col in get and isinstance(get.get(col), list):
                                return get.get(col) or []
                            # 如果 collection_name 不是直接 key，也兜底取第一个 list
                            for _k, v in get.items():
                                if isinstance(v, list):
                                    return v
                return []

            def _extract_props(obj: Any) -> dict[str, Any]:
                if obj is None:
                    return {}
                props_attr = getattr(obj, "properties", None)
                if props_attr is not None:
                    return props_attr or {}
                if isinstance(obj, dict):
                    # 可能是 {"properties": {...}} 或扁平 {"entity_id": ...}
                    if isinstance(obj.get("properties"), dict):
                        return obj.get("properties") or {}
                    return obj
                return {}

            def _extract_distance(obj: Any) -> Any:
                # SDK 对象：obj.metadata.distance
                dist_attr = getattr(getattr(obj, "metadata", None), "distance", None)
                if dist_attr is not None:
                    return dist_attr
                if isinstance(obj, dict):
                    md = obj.get("metadata") or {}
                    if isinstance(md, dict) and "distance" in md:
                        return md.get("distance")
                    # 有些返回可能把 distance 放到额外字段里（兜底）
                    if "distance" in obj:
                        return obj.get("distance")
                return None

            objs = _extract_objects(result)
            out: list[tuple[str, float]] = []
            # 诊断：记录抽取到的候选对象数量 + 前几个对象的字段形态
            debug_parts: list[str] = []
            debug_parts.append(f"extracted_objs_len={len(objs)}")
            if isinstance(result, dict):
                debug_parts.append(f"raw_result_keys={list(result.keys())[:20]}")
            debug_parts.append(f"raw_result_type={type(result).__name__}")
            for obj in objs:
                props = _extract_props(obj)
                eid = props.get("entity_id") or ""
                dist = _extract_distance(obj)
                # cosine distance 越小越相似：用 1-distance 转成“越大越相似”的分数（若 dist 缺失则分数为 0）
                score = 1.0 - float(dist) if dist is not None else 0.0
                if str(eid):
                    out.append((str(eid), score))

            # 额外诊断：如果返回对象不少但 entity_id 取不到，说明 properties 形态可能不一致
            if len(objs) > 0 and len(out) == 0:
                sample = objs[:3]
                sample_bits: list[str] = []
                for i, s in enumerate(sample):
                    sp = _extract_props(s)
                    sd = _extract_distance(s)
                    sample_bits.append(
                        f"sample{i}: props_keys={list(sp.keys())[:10]} entity_id={sp.get('entity_id','')} distance={sd}"
                    )
                debug_parts.append("entity_id_missing_or_empty=" + "; ".join(sample_bits))

            self._last_search_detail = " | ".join(debug_parts)

            return out
        except Exception:
            # 记录异常信息供 UI 展示
            import traceback

            self._last_search_error = traceback.format_exc()
            return []

    def search_by_text(self, query_text: str, top_k: int = 10) -> list[tuple[str, float]]:
        vec = get_embedding(query_text, self._dim)
        return self.search_by_vector(vec, top_k=top_k)

    def get_by_entity_id(self, entity_id: str) -> Optional[dict[str, Any]]:
        """按 entity_id（图谱方法节点 id）取 Weaviate 中对应对象，用于「从方法查源代码」双向关联。"""
        eid = (entity_id or "").strip()
        if not eid:
            return None
        if eid.startswith("method://") or eid.startswith("method//"):
            candidates = method_entity_id_variants(eid) or [eid]
        else:
            candidates = [eid]
        try:
            from weaviate.classes.query import Filter
            coll = self._get_collection()
            for cand in candidates:
                result = coll.query.fetch_objects(
                    filters=Filter.by_property("entity_id").equal(cand),
                    limit=1,
                )
                for obj in result.objects:
                    return {
                        "entity_id": obj.properties.get("entity_id", cand),
                        "name": obj.properties.get("name", ""),
                        "entity_type": obj.properties.get("entity_type", ""),
                        "code_snippet": obj.properties.get("code_snippet", ""),
                    }
            return None
        except Exception:
            return None

    # clear/close/__del__ 由 BaseWeaviateStore 提供
