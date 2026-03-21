"""业务解读：独立 Weaviate collection，按 entity_id + level 存业务综述文本 + 向量。"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from src.core.weaviate_defaults import (
    DEFAULT_COLLECTION_BUSINESS_INTERPRETATION,
    DEFAULT_WEAVIATE_GRPC_PORT,
    DEFAULT_WEAVIATE_HTTP_URL,
)
from src.knowledge.base_weaviate_store import BaseWeaviateStore
from src.knowledge.method_entity_id_normalize import (
    method_entity_id_variants,
    normalize_method_entity_id,
)


class WeaviateBusinessInterpretStore(BaseWeaviateStore):
    """collection：BusinessInterpretation；按 (entity_id, level) 存业务解读。"""

    def __init__(
        self,
        url: str = DEFAULT_WEAVIATE_HTTP_URL,
        grpc_port: int = DEFAULT_WEAVIATE_GRPC_PORT,
        collection_name: str = DEFAULT_COLLECTION_BUSINESS_INTERPRETATION,
        dimension: int = 1024,
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
            Property(name="entity_id", data_type=DataType.TEXT),
            Property(name="entity_type", data_type=DataType.TEXT),
            Property(name="level", data_type=DataType.TEXT),  # class | api | module
            Property(name="business_domain", data_type=DataType.TEXT),
            Property(name="business_capabilities", data_type=DataType.TEXT),
            Property(name="summary_text", data_type=DataType.TEXT),
            Property(name="language", data_type=DataType.TEXT),
            Property(name="context_json", data_type=DataType.TEXT),
            Property(name="related_entity_ids_json", data_type=DataType.TEXT),
        ]

    def add(
        self,
        vector: list[float],
        entity_id: str,
        level: str,
        summary_text: str,
        *,
        entity_type: str = "",
        business_domain: str = "",
        business_capabilities: str = "",
        language: str = "zh",
        context_json: str = "",
        related_entity_ids_json: str = "",
    ) -> bool:
        """写入一条业务解读，成功返回 True，失败返回 False。已存在则 upsert 覆盖。"""
        ok, _created = self.add_with_created(
            vector=vector,
            entity_id=entity_id,
            level=level,
            summary_text=summary_text,
            entity_type=entity_type,
            business_domain=business_domain,
            business_capabilities=business_capabilities,
            language=language,
            context_json=context_json,
            related_entity_ids_json=related_entity_ids_json,
        )
        return ok

    def add_with_created(
        self,
        vector: list[float],
        entity_id: str,
        level: str,
        summary_text: str,
        *,
        entity_type: str = "",
        business_domain: str = "",
        business_capabilities: str = "",
        language: str = "zh",
        context_json: str = "",
        related_entity_ids_json: str = "",
    ) -> tuple[bool, bool]:
        """
        写入一条业务解读并返回 (成功, 是否新建)。
        - 成功：insert 或 replace 没报错
        - 是否新建：仅首次 insert 创建新对象为 True；已存在则 replace 为 False
        """
        if not vector or len(vector) < self._dim:
            return False, False
        coll = self._get_collection()
        uid = self._to_uuid(entity_id + "|" + (level or "biz"))
        props = {
            "entity_id": entity_id,
            "entity_type": entity_type or "",
            "level": level or "",
            "business_domain": (business_domain or "")[:500],
            "business_capabilities": (business_capabilities or "")[:2000],
            "summary_text": (summary_text or "")[:48000],
            "language": language or "zh",
            "context_json": (context_json or "")[:12000],
            "related_entity_ids_json": (related_entity_ids_json or "")[:8000],
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
                        "Weaviate 业务解读 replace 失败 (entity=%s, level=%s): %s",
                        entity_id[:50] if entity_id else "?",
                        level,
                        e2,
                    )
                    return False, False
            logging.getLogger(__name__).warning(
                "Weaviate 业务解读写入失败 (entity=%s, level=%s): %s",
                entity_id[:50] if entity_id else "?",
                level,
                e,
            )
            return False, False

    def get_by_entity(self, entity_id: str, level: Optional[str] = None) -> Optional[dict[str, Any]]:
        if not entity_id or not self._client:
            return None
        try:
            from weaviate.classes.query import Filter

            coll = self._get_collection()
            for eid_try in method_entity_id_variants(entity_id):
                flt = Filter.by_property("entity_id").equal(eid_try)
                if level:
                    flt = flt & Filter.by_property("level").equal(level)
                result = coll.query.fetch_objects(filters=flt, limit=1)
                for obj in result.objects:
                    p = obj.properties or {}
                    return {
                        "entity_id": p.get("entity_id", eid_try),
                        "entity_type": p.get("entity_type", ""),
                        "level": p.get("level", ""),
                        "summary_text": p.get("summary_text", ""),
                        "business_domain": p.get("business_domain", ""),
                        "business_capabilities": p.get("business_capabilities", ""),
                        "language": p.get("language", ""),
                        "context_json": p.get("context_json", ""),
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
                return len(self.list_existing_entity_level_pairs(limit=200000))
            except Exception:
                return 0

    def list_existing_entity_level_pairs(self, limit: int = 200000) -> set[tuple[str, str]]:
        """(entity_id, level) 集合，用于业务解读断点续跑时跳过已有记录。"""
        pairs: set[tuple[str, str]] = set()
        if not self._client:
            return pairs
        try:
            coll = self._get_collection()
            # 分页抓取：避免一次性 limit 很大导致 SDK/服务端抛异常或返回空 properties
            page_size = 2000
            fetched = 0
            target = max(0, int(limit))
            while fetched < target:
                cur_limit = min(page_size, target - fetched)
                try:
                    result = coll.query.fetch_objects(
                        limit=cur_limit,
                        offset=fetched,
                        return_properties=["entity_id", "level"],
                    )
                except TypeError:
                    # offset 或 return_properties 不支持：退化为单页拉取一次
                    try:
                        result = coll.query.fetch_objects(
                            limit=target,
                            return_properties=["entity_id", "level"],
                        )
                    except TypeError:
                        result = coll.query.fetch_objects(limit=target)
                    for obj in (result.objects or []):
                        p = obj.properties or {}
                        eid = p.get("entity_id")
                        lv = p.get("level")
                        if isinstance(eid, str) and eid and isinstance(lv, str) and lv:
                            pairs.add((eid, lv))
                    break

                objs = result.objects or []
                if not objs:
                    break
                for obj in objs:
                    p = obj.properties or {}
                    eid = p.get("entity_id")
                    lv = p.get("level")
                    if isinstance(eid, str) and eid and isinstance(lv, str) and lv:
                        pairs.add((eid, lv))
                fetched += len(objs)
                if len(objs) < cur_limit:
                    break
        except Exception:
            return pairs
        return pairs

    def list_by_level(self, level: str, limit: int = 100) -> list[dict[str, Any]]:
        """按 level（class/api/module 等）列出若干业务解读摘要。"""
        if not level or not self._client:
            return []
        try:
            from weaviate.classes.query import Filter

            coll = self._get_collection()
            result = coll.query.fetch_objects(
                filters=Filter.by_property("level").equal(level),
                limit=limit,
            )
            out: list[dict[str, Any]] = []
            for obj in result.objects:
                p = obj.properties or {}
                out.append(
                    {
                        "entity_id": p.get("entity_id", ""),
                        "entity_type": p.get("entity_type", ""),
                        "level": p.get("level", ""),
                        "summary_text": p.get("summary_text", ""),
                        "business_domain": p.get("business_domain", ""),
                        "business_capabilities": p.get("business_capabilities", ""),
                        "language": p.get("language", ""),
                    }
                )
            return out
        except Exception:
            return []

    def search_method_hits_by_text(self, query_text: str, top_k: int = 10) -> list[tuple[str, float]]:
        """
        在业务解读库中按向量检索，仅保留 level=api 且 entity_id 为 method// 或 method:// 的记录，
        返回 (method_entity_id, score)，同一方法取最高 score；id 统一为 method:// 以便与技术解读/图谱对齐。

        小体量 collection（如仅数百条）在 Weaviate filter 异常或「无 filter 回退」时曾导致结果全被
        class/module 占满、Python 侧过滤后恒为空。此处对 total 较小时改为 **不做 Weaviate filter**、
        一次拉全库近邻序再在 Python 里筛 level=api + method*，保证业务解读能进合并池。
        """
        from src.semantic.embedding import get_embedding
        from src.knowledge.weaviate_near_vector import near_vector_property_hits
        from weaviate.classes.query import Filter

        if not (query_text or "").strip() or not self._client:
            return []
        vec = get_embedding(query_text.strip(), self._dim)
        coll = self._get_collection()
        # 全库规模不大时：limit=总数，无 Weaviate filter，避免 filter 失败链路问题
        _PY_FILTER_MAX = 10_000
        total = 0
        try:
            total = int(coll.aggregate.over_all(total_count=True).total_count or 0)
        except Exception:
            total = self.count()
        use_python_only = 0 < total <= _PY_FILTER_MAX
        if use_python_only:
            fetch_limit = max(total, int(top_k), 1)
            flt = None
        else:
            fetch_limit = min(max(int(top_k) * 8, int(top_k), 100), 2000)
            flt = Filter.by_property("level").equal("api")
        rows = near_vector_property_hits(
            coll,
            vector=vec,
            dim=self._dim,
            limit=fetch_limit,
            collection_name=self._collection_name,
            return_properties=[
                "entity_id",
                "level",
                "entity_type",
                "summary_text",
            ],
            filters=flt,
        )
        merged: dict[str, float] = {}
        for props, score in rows:
            eid_raw = str(props.get("entity_id") or "").strip()
            lv = str(props.get("level") or "").strip().lower()
            if lv != "api":
                continue
            if not (eid_raw.startswith("method://") or eid_raw.startswith("method//")):
                continue
            eid = normalize_method_entity_id(eid_raw)
            merged[eid] = max(merged.get(eid, 0.0), float(score))
        sorted_ids = sorted(merged.keys(), key=lambda k: merged[k], reverse=True)
        return [(k, merged[k]) for k in sorted_ids[: int(top_k)]]

    # clear/close/__del__ 由 BaseWeaviateStore 提供

