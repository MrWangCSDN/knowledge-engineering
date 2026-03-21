"""Weaviate v4 near_vector 查询与结果解析（多 collection 复用，避免 return_properties 缺失导致 objects 为空）。"""
from __future__ import annotations

import logging
from typing import Any, Optional

_log = logging.getLogger(__name__)


def near_vector_property_hits(
    coll: Any,
    *,
    vector: list[float],
    dim: int,
    limit: int,
    collection_name: str,
    return_properties: list[str],
    filters: Any | None = None,
) -> list[tuple[dict[str, Any], float]]:
    """
    对 collection 做 near_vector，返回 (properties 字典, score) 列表。
    score 为 1 - cosine_distance（越大越相似）；distance 缺失时为 0。
    """
    if not vector or len(vector) < dim:
        return []

    from weaviate.classes.query import MetadataQuery

    vec = vector[:dim]
    metadata = MetadataQuery(distance=True)
    base_kw: dict[str, Any] = dict(
        near_vector=vec,
        limit=int(limit),
        return_properties=return_properties,
        return_metadata=metadata,
    )

    result: Any = None
    if filters is not None:
        try:
            q = coll.query.near_vector(**base_kw, filters=filters)
            result = q.do() if hasattr(q, "do") else q
        except TypeError as e:
            _log.warning(
                "near_vector+filters TypeError（collection=%s）: %s；不再回退无 filter，避免 class/module 占满结果。",
                collection_name,
                e,
            )
            return []
        except Exception as e:
            _log.warning(
                "near_vector+filters 失败（collection=%s）: %s；不再回退无 filter。",
                collection_name,
                e,
            )
            return []
    else:
        try:
            q = coll.query.near_vector(**base_kw)
            result = q.do() if hasattr(q, "do") else q
        except Exception as e:
            _log.warning("near_vector 失败（collection=%s）: %s", collection_name, e)
            return []

    if result is None:
        return []

    objs = _extract_objects(result, collection_name)
    out: list[tuple[dict[str, Any], float]] = []
    for obj in objs:
        props = _extract_props(obj)
        dist = _extract_distance(obj)
        score = 1.0 - float(dist) if dist is not None else 0.0
        out.append((props, score))
    return out


def _extract_objects(maybe_result: Any, collection_name: str) -> list[Any]:
    if maybe_result is None:
        return []
    objs_attr = getattr(maybe_result, "objects", None)
    if objs_attr is not None:
        return list(objs_attr or [])
    if isinstance(maybe_result, list):
        return maybe_result
    if isinstance(maybe_result, dict):
        if isinstance(maybe_result.get("objects"), list):
            return maybe_result.get("objects") or []
        data = maybe_result.get("data") or maybe_result.get("Data") or {}
        if isinstance(data, dict):
            get = data.get("Get") or data.get("get") or {}
            if isinstance(get, dict):
                if collection_name in get and isinstance(get.get(collection_name), list):
                    return get.get(collection_name) or []
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
        if isinstance(obj.get("properties"), dict):
            return obj.get("properties") or {}
        return obj
    return {}


def _extract_distance(obj: Any) -> Optional[float]:
    dist_attr = getattr(getattr(obj, "metadata", None), "distance", None)
    if dist_attr is not None:
        return float(dist_attr)
    if isinstance(obj, dict):
        md = obj.get("metadata") or {}
        if isinstance(md, dict) and "distance" in md:
            v = md.get("distance")
            return float(v) if v is not None else None
        if "distance" in obj:
            v = obj.get("distance")
            return float(v) if v is not None else None
    return None
