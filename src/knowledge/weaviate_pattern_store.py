"""模式识别：独立 Weaviate collection（支持 system/global 与 module 级结果）。"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from src.core.weaviate_defaults import (
    DEFAULT_COLLECTION_PATTERN_INTERPRETATION,
    DEFAULT_WEAVIATE_GRPC_PORT,
    DEFAULT_WEAVIATE_HTTP_URL,
)
from src.knowledge.base_weaviate_store import BaseWeaviateStore

_LOG = logging.getLogger(__name__)


class WeaviatePatternInterpretStore(BaseWeaviateStore):
    """collection：PatternInterpretation

    - scope_type: "system" | "module"
    - target_id: system 固定为 "system"，module 级为 module_id
    - 每条 pattern 作为一个对象：可用 upsert 覆盖重跑结果
    """

    def __init__(
        self,
        *,
        url: str = DEFAULT_WEAVIATE_HTTP_URL,
        grpc_port: int = DEFAULT_WEAVIATE_GRPC_PORT,
        collection_name: str = DEFAULT_COLLECTION_PATTERN_INTERPRETATION,
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
            Property(name="scope_type", data_type=DataType.TEXT),  # system | module
            Property(name="target_id", data_type=DataType.TEXT),  # system | module_id
            Property(name="pattern_type", data_type=DataType.TEXT),  # design | architecture
            Property(name="pattern_name", data_type=DataType.TEXT),
            Property(name="confidence", data_type=DataType.TEXT),  # 用字符串存，避免 DataType NUMBER 差异
            Property(name="summary_text", data_type=DataType.TEXT),
            Property(name="evidence_json", data_type=DataType.TEXT),
            Property(name="language", data_type=DataType.TEXT),
            Property(name="related_entity_ids_json", data_type=DataType.TEXT),
        ]

    def add(
        self,
        vector: list[float],
        *,
        scope_type: str,
        target_id: str,
        pattern_type: str,
        pattern_name: str,
        confidence: float,
        summary_text: str,
        evidence_json: str = "",
        language: str = "zh",
        related_entity_ids_json: str = "[]",
    ) -> bool:
        ok, _created = self.add_with_created(
            vector,
            scope_type=scope_type,
            target_id=target_id,
            pattern_type=pattern_type,
            pattern_name=pattern_name,
            confidence=confidence,
            summary_text=summary_text,
            evidence_json=evidence_json,
            language=language,
            related_entity_ids_json=related_entity_ids_json,
        )
        return ok

    def add_with_created(
        self,
        vector: list[float],
        *,
        scope_type: str,
        target_id: str,
        pattern_type: str,
        pattern_name: str,
        confidence: float,
        summary_text: str,
        evidence_json: str = "",
        language: str = "zh",
        related_entity_ids_json: str = "[]",
    ) -> tuple[bool, bool]:
        """写入单条 pattern，并返回 (success, created)。"""
        if not vector or len(vector) < self._dim:
            return False, False

        scope_type = (scope_type or "").strip().lower() or "system"
        pattern_type = (pattern_type or "").strip().lower() or "design"
        target_id = (target_id or "").strip() or "system"
        pattern_name = (pattern_name or "").strip() or "Unknown"

        coll = self._get_collection()
        uid = self._to_uuid(f"{target_id}|{scope_type}|{pattern_type}|{pattern_name}|pattern")

        # weaviate 属性字段很多会在 schema 层被截断；这里做显式截断减少无意义超长请求
        props = {
            "scope_type": scope_type,
            "target_id": target_id,
            "pattern_type": pattern_type,
            "pattern_name": pattern_name[:200],
            "confidence": str(float(confidence))[:20],
            "summary_text": (summary_text or "")[:16000],
            "evidence_json": (evidence_json or "")[:30000],
            "language": language or "zh",
            "related_entity_ids_json": related_entity_ids_json[:8000],
        }

        vec = vector[: self._dim]
        try:
            # Weaviate 1.24+ named vectors：dict 形式 {"default": [...]}
            coll.data.insert(properties=props, vector={"default": vec}, uuid=uid)
            return True, True
        except Exception as e:
            if "already exists" in str(e).lower() or "422" in str(e):
                try:
                    coll.data.replace(uuid=uid, properties=props, vector={"default": vec})
                    return True, False
                except Exception as e2:
                    _LOG.warning("Weaviate pattern replace 失败: %s", e2)
                    return False, False
            _LOG.warning("Weaviate pattern 写入失败: %s", e)
            return False, False

    def list_by_scope(self, *, scope_type: str, target_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """按 scope_type + target_id 列出 pattern 结果。"""
        scope_type = (scope_type or "").strip().lower() or "system"
        target_id = (target_id or "").strip() or "system"
        if not self._client:
            return []
        try:
            from weaviate.classes.query import Filter

            coll = self._get_collection()
            flt = Filter.by_property("scope_type").equal(scope_type) & Filter.by_property("target_id").equal(target_id)
            result = coll.query.fetch_objects(filters=flt, limit=int(limit))
            out: list[dict[str, Any]] = []
            for obj in result.objects or []:
                p = obj.properties or {}
                out.append(
                    {
                        "scope_type": p.get("scope_type", scope_type),
                        "target_id": p.get("target_id", target_id),
                        "pattern_type": p.get("pattern_type", ""),
                        "pattern_name": p.get("pattern_name", ""),
                        "confidence": p.get("confidence", "0"),
                        "summary_text": p.get("summary_text", ""),
                        "evidence_json": p.get("evidence_json", ""),
                        "language": p.get("language", ""),
                        "related_entity_ids_json": p.get("related_entity_ids_json", "[]"),
                    }
                )
            return out
        except Exception:
            return []

    def list_existing_target_ids(self, scope_type: str, limit: int = 100000) -> set[str]:
        """列出某 scope_type 下已有的 target_id 集合（用于跳过已识别过的 module）。"""
        if not self._client:
            return set()
        scope_type = (scope_type or "").strip().lower() or "system"
        try:
            from weaviate.classes.query import Filter

            coll = self._get_collection()

            page_size = 2000
            fetched = 0
            target = max(0, int(limit))
            out: set[str] = set()

            while fetched < target:
                cur_limit = min(page_size, target - fetched)
                try:
                    result = coll.query.fetch_objects(
                        filters=Filter.by_property("scope_type").equal(scope_type),
                        limit=cur_limit,
                        offset=fetched,
                        return_properties=["target_id"],
                    )
                except TypeError:
                    # offset / return_properties 不兼容时退化为简单拉取一次
                    result = coll.query.fetch_objects(
                        filters=Filter.by_property("scope_type").equal(scope_type),
                        limit=target,
                        return_properties=["target_id"],
                    )

                objs = result.objects or []
                if not objs:
                    break

                for obj in objs:
                    p = obj.properties or {}
                    tid = p.get("target_id")
                    if isinstance(tid, str) and tid:
                        out.add(tid)

                fetched += len(objs)
                if len(objs) < cur_limit:
                    break

            return out
        except Exception:
            return set()

    def add_many_encoded_evidence(
        self,
        vector: list[float],
        *,
        scope_type: str,
        target_id: str,
        pattern_type: str,
        pattern_name: str,
        confidence: float,
        summary_text: str,
        evidence: Any,
        language: str = "zh",
        related_entity_ids: Optional[list[str]] = None,
    ) -> bool:
        evidence_json = ""
        try:
            evidence_json = json.dumps(evidence, ensure_ascii=False)
        except Exception:
            evidence_json = str(evidence)

        related = related_entity_ids or []
        try:
            related_json = json.dumps(related, ensure_ascii=False)
        except Exception:
            related_json = "[]"

        return self.add(
            vector,
            scope_type=scope_type,
            target_id=target_id,
            pattern_type=pattern_type,
            pattern_name=pattern_name,
            confidence=confidence,
            summary_text=summary_text,
            evidence_json=evidence_json,
            language=language,
            related_entity_ids_json=related_json,
        )

