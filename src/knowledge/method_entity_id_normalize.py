"""方法实体 ID：结构层多为 method//，展示/图谱侧常见 method://，检索合并时需统一并可多形态查询。"""
from __future__ import annotations


def normalize_method_entity_id(eid: str) -> str:
    """统一为 method://{hash}，便于与技术解读/图谱展示对齐。"""
    s = (eid or "").strip()
    if s.startswith("method://"):
        return s
    if s.startswith("method//"):
        return "method://" + s[len("method//") :]
    return s


def method_entity_id_variants(eid: str) -> list[str]:
    """Weaviate / 流水线写入可能是 method// 或 method://，查询时依次尝试。"""
    s = (eid or "").strip()
    if not s:
        return []
    out: list[str] = [s]
    if s.startswith("method://"):
        alt = "method//" + s[len("method://") :]
        if alt not in out:
            out.append(alt)
    elif s.startswith("method//"):
        alt = "method://" + s[len("method//") :]
        if alt not in out:
            out.append(alt)
    return out
