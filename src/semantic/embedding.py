"""语义层向量化：将 embed_text 转为向量，供知识层存储与检索。可选 sentence-transformers，否则使用确定性伪向量。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional
import urllib.error
import urllib.request

# 默认向量维度：与 Ollama bge-m3 输出对齐（通常为 1024）
DEFAULT_DIM = 1024

_ollama_cfg: Optional[dict] = None


def get_embedding(text: str, dimension: int = DEFAULT_DIM) -> list[float]:
    """
    将文本转为固定维度向量。
    优先通过本地 Ollama 的 embedding 模型（如 bge-m3）；失败时回退到确定性伪向量（同文本同向量）。
    """
    if not text or not text.strip():
        return [0.0] * dimension
    try:
        vec = _ollama_embedding(text)
        if not vec:
            return _hash_vector(text, dimension)
        # 根据调用方期望的维度做截断/补零（保持向量长度一致）
        if len(vec) > dimension:
            return vec[:dimension]
        if len(vec) < dimension:
            return vec + [0.0] * (dimension - len(vec))
        return vec
    except Exception:
        return _hash_vector(text, dimension)


def _load_ollama_cfg() -> dict:
    """
    从 config/project.yaml 中加载 embedding 相关配置。
    若加载失败或缺少配置，则使用内置默认值。
    """
    global _ollama_cfg
    if _ollama_cfg is not None:
        return _ollama_cfg
    base = Path(__file__).resolve().parents[2]  # 项目根
    cfg_path = base / "config" / "project.yaml"
    cfg: dict = {}
    try:
        import yaml

        if cfg_path.exists():
            raw = cfg_path.read_text(encoding="utf-8")
            cfg = yaml.safe_load(raw) or {}
    except Exception:
        cfg = {}
    knowledge = cfg.get("knowledge") or {}
    sem = knowledge.get("semantic_embedding") or {}
    _ollama_cfg = {
        "base_url": sem.get("ollama_base_url") or "http://127.0.0.1:11434",
        "model": sem.get("ollama_model") or "bge-m3",
    }
    return _ollama_cfg


def _ollama_embedding(text: str) -> list[float]:
    """
    通过本地 Ollama /api/embeddings 获取向量。
    返回原始 embedding 列表；调用方负责截断/补零到目标维度。
    """
    cfg = _load_ollama_cfg()
    base = (cfg.get("base_url") or "http://127.0.0.1:11434").rstrip("/")
    model = cfg.get("model") or "bge-m3"
    url = base + "/api/embeddings"
    payload = {"model": model, "input": text}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            js = json.loads(raw)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError):
        return []
    vec = None
    try:
        # Ollama embeddings 一般返回 {"data": [{"embedding": [...]}], ...}
        items = js.get("data") or []
        if items:
            vec = items[0].get("embedding")
    except Exception:
        vec = None
    if not isinstance(vec, list) or not vec:
        return []
    # 确保为 float 列表
    out: list[float] = []
    for x in vec:
        try:
            out.append(float(x))
        except Exception:
            out.append(0.0)
    return out


def _hash_vector(text: str, dimension: int) -> list[float]:
    """确定性伪向量：同一文本得到相同向量，便于复现与测试。"""
    out = [0.0] * dimension
    h = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    for i in range(dimension):
        sub = h[(i * 2) % len(h) : (i * 2 + 4) % len(h) + 4] or "0"
        out[i] = (int(sub, 16) % 10000) / 5000.0 - 1.0
    norm = (sum(x * x for x in out)) ** 0.5
    if norm > 1e-9:
        out = [x / norm for x in out]
    return out


def compute_embedding_id(entity_id: str, text: str) -> str:
    """为 (实体, 文本) 生成稳定 ID，用于向量库去重或索引。"""
    raw = entity_id + "|" + (text or "")
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"vec://{h}"


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """余弦相似度。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return dot / (na * nb)
