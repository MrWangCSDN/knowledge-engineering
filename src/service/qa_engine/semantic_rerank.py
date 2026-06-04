"""召回二次语义重排（gte-rerank + 门控式护栏）。

设计 [[召回二次重排-设计]]：cosine 不自信时才调 DashScope gte-rerank 重排候选顺序，
吃自然提问 recall@1 翻倍的上行、不伤 cosine 已对的简单题（门控式护栏）。门控（architecture/
chit-chat）解耦：本模块只改候选顺序，不碰 score、不参与门控。
"""
from __future__ import annotations  # 注解延迟求值

import logging  # 标准库：降级时记 warning
import os       # 读 env 阈值/开关/API key

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
