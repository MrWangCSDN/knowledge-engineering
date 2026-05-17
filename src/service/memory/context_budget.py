"""会话上下文 token 预算 / 历史裁剪（spec §18）。纯函数，无 DB/IO。

对齐 Claude Code：按模型窗口 token 压力决定保留多少最近原文轮；
更早轮由 system 记忆块的 working_summary+focus 顶替（不在此模块，在 recall）。
"""
from __future__ import annotations

import math
import os

_DEFAULT_WINDOW = 128000          # 保守默认（Fork B；env KE_MODEL_CONTEXT_WINDOW 覆盖）
_DEFAULT_RESERVE_PCT = 0.45       # 预留 system+记忆块+KG context+本轮问题+回答


def estimate_tokens(text: str) -> int:
    """粗算：1 token ≈ 1.5 字符（中英混合），向上取整，保守偏大（Fork A）。"""
    if not text:
        return 0
    return math.ceil(len(text) / 1.5)


def model_context_window() -> int:
    """模型上下文窗口（token）。env `KE_MODEL_CONTEXT_WINDOW` 覆盖；
    缺失/非法/非正 → 保守默认 128000。"""
    raw = os.getenv("KE_MODEL_CONTEXT_WINDOW", "").strip()
    try:
        v = int(raw)
        if v > 0:
            return v
    except (TypeError, ValueError):
        pass
    return _DEFAULT_WINDOW


def _reserve_pct() -> float:
    raw = os.getenv("KE_CTX_RESERVE_PCT", "").strip()
    try:
        v = float(raw)
        if 0.0 < v < 1.0:
            return v
    except (TypeError, ValueError):
        pass
    return _DEFAULT_RESERVE_PCT


def history_token_budget() -> int:
    """留给『原文历史』的 token 预算 = 窗口 × (1 − 预留比例)。"""
    return int(model_context_window() * (1.0 - _reserve_pct()))


def trim_history_to_budget(
    history, budget: int
) -> tuple[list[dict], int]:
    """保留最近若干轮使累计估算 token ≤ budget；更早轮丢弃（由 summary 顶替）。

    返回 (保留的最近列表[保持原序], 估算token)。防御：history 非 list/空/budget≤0
    → ([],0)；即便最近 1 条已超预算，也至少保留它（绝不返回空历史）。
    """
    if not isinstance(history, list) or not history or budget <= 0:
        return ([], 0)
    kept_rev: list[dict] = []
    used = 0
    for m in reversed(history):
        if not isinstance(m, dict):
            continue
        t = estimate_tokens(str(m.get("content", "")))
        if kept_rev and used + t > budget:
            break
        kept_rev.append(m)
        used += t
    kept_rev.reverse()
    return (kept_rev, used)
