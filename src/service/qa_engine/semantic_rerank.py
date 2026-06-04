"""召回二次语义重排（gte-rerank + 门控式护栏）。

设计 [[召回二次重排-设计]]：cosine 不自信时才调 DashScope gte-rerank 重排候选顺序，
吃自然提问 recall@1 翻倍的上行、不伤 cosine 已对的简单题（门控式护栏）。门控（architecture/
chit-chat）解耦：本模块只改候选顺序，不碰 score、不参与门控。
"""
from __future__ import annotations  # 注解延迟求值

import logging  # 标准库：降级时记 warning
import os       # 读 env 阈值/开关/API key

import requests  # 第三方：HTTP 调 DashScope rerank（与 embedding.py 同款）

_log = logging.getLogger(__name__)

# 门控式护栏阈值（设计 §4）：判 cosine 是否"自信"
_DEFAULT_CONFIDENT_TOP1 = 0.6   # top1 cosine ≥ 此值才算 top1 够强
_DEFAULT_MARGIN = 0.05          # top1 - top2 ≥ 此值才算与 top2 拉开


def _env_float(key: str, default: float) -> float:
    """读 env 浮点阈值；缺失 / 非法 → default。"""
    raw = os.environ.get(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def should_rerank(candidates: list[dict]) -> bool:
    """门控式护栏：cosine 不自信时才需要 rerank（候选已按 cosine score 降序）。

    规则（设计 §4）：
      - 候选 < 2 → False（无可重排）
      - top1 < CONFIDENT_TOP1 → True（top1 本身不高，不确定）
      - top1 - top2 < MARGIN → True（top1/top2 接近，谁第一不明确）
      - 否则 → False（top1 高且拉开，信 cosine，避翻车 + 省调用）
    """
    if len(candidates) < 2:
        return False
    confident = _env_float("KE_RERANK_CONFIDENT_TOP1", _DEFAULT_CONFIDENT_TOP1)
    margin = _env_float("KE_RERANK_MARGIN", _DEFAULT_MARGIN)
    # .get("score", 0.0)：缺分数（理论上不会，adapter 必填）→ 0.0 → 视为不自信偏保守
    top1 = candidates[0].get("score", 0.0)
    top2 = candidates[1].get("score", 0.0)
    if top1 < confident:
        return True
    if (top1 - top2) < margin:
        return True
    return False


# DashScope gte-rerank（cross-encoder）HTTP 端点 + 模型（与 embedding 同账号 key）
_RERANK_URL = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
_RERANK_MODEL = "gte-rerank-v2"
_RERANK_TIMEOUT = 5  # 秒；超时即降级回退 cosine 序，不拖慢召回


def _gte_rerank(query: str, documents: list[str], api_key: str) -> list[int]:
    """调 DashScope gte-rerank，返回按相关度降序的"原始 index"列表。

    Args:
        query: 用户原始问题（cross-encoder 吃完整意图）
        documents: 候选文本列表（与候选同序，index 一一对应）
        api_key: DASHSCOPE_API_KEY
    Returns:
        原始 index 的相关度降序列表（如 [3,0,5,...]）；调用方据此重排候选。
    Raises:
        requests / 解析异常透传（由 rerank_candidates 捕获降级）。
    """
    payload = {
        "model": _RERANK_MODEL,
        "input": {"query": query, "documents": documents},
        # return_documents=False：只要排序不要回传文本；top_n 全量
        "parameters": {"return_documents": False, "top_n": len(documents)},
    }
    resp = requests.post(
        _RERANK_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=_RERANK_TIMEOUT,
    )
    resp.raise_for_status()  # 非 2xx 抛 → 上层降级
    # output.results 已按 relevance_score 降序；每项 {"index": i, "relevance_score": s}
    results = resp.json()["output"]["results"]
    return [r["index"] for r in results]


def _rerank_enabled() -> bool:
    """env 开关 KE_RECALL_RERANK：默认 on；"0"/"false"/"off"/"no" 关。"""
    raw = (os.environ.get("KE_RECALL_RERANK") or "").strip().lower()
    return raw not in ("0", "false", "off", "no")


def rerank_candidates(question: str, candidates: list[dict]) -> list[dict]:
    """用 gte-rerank 重排候选顺序（best-effort，绝不抛、绝不改 score 字段）。

    降级条件（任一 → 原样返回 cosine 序）：env 关 / 候选<2 / 无 API key / 任何异常。
    """
    if not _rerank_enabled():
        return candidates
    if len(candidates) < 2:
        return candidates
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        return candidates
    # 候选配对文本：2b 解读优先，空则 entity_id；截 ≤500 控 payload
    docs = [(c.get("summary_text") or c.get("entity_id") or "")[:500] for c in candidates]
    try:
        order = _gte_rerank(question, docs, api_key)
    except Exception as e:
        # 网络 / 超时 / 返回格式异常都降级回退原序，绝不阻断召回
        _log.warning("gte-rerank 失败，回退 cosine 序: %s", e)
        return candidates
    # 按相关度降序 index 重排；防御越界/重复/缺失 index（漏掉的候选按原序补末尾）
    seen: set[int] = set()
    reranked: list[dict] = []
    for i in order:
        if isinstance(i, int) and 0 <= i < len(candidates) and i not in seen:
            seen.add(i)
            reranked.append(candidates[i])
    for i, c in enumerate(candidates):
        if i not in seen:
            reranked.append(c)
    return reranked
