"""记忆系统 P1 核心逻辑：召回 / 显式意图检测 / 写入 / 会话压缩。

设计：[[记忆系统-设计]] §4.1 §4.3 §6 §7。
纯逻辑，不依赖 FastAPI；DB 用 duck-typed AsyncSession（真跑用 SQLAlchemy，
单测用 Fake），LLM 用 duck-typed provider（有 async complete(system,user)）。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from src.service.db_models_homepage import QAUserMemory, QASessionMemory, QAMessage
from src.service.qa_engine.prompts import _SESSION_COMPACT_SYSTEM

_log = logging.getLogger(__name__)


# 显式记忆触发词（关键词起步；spec §15 开放问题留 P1 用关键词）
# 命中后剥掉触发词 + 紧随的冒号/空白，剩余即记忆内容。
# ⚠️ 顺序约定：若将来新增触发词是已有词的「超串」（如「顺便记一下」含「记一下」），
#    必须把更具体的放前面，否则被较短前缀先匹配（当前 5 个互不为前缀，安全）。
_TRIGGERS = ("请记住", "记住", "记一下", "记下", "帮我记住")


def detect_explicit_memory(question: str) -> str | None:
    """从用户问题里检测显式记忆意图。

    命中触发词 → 返回剥离触发词后的内容（去首尾空白与起始的中英文冒号）。
    未命中 / 内容为空 → None。
    """
    q = (question or "").strip()
    for trig in _TRIGGERS:
        if q.startswith(trig):
            rest = q[len(trig):]
            rest = rest.lstrip(" :：\t").strip()
            return rest or None
    return None


async def recall_memory_block(db: Any, *, user_id: int, session_id: str) -> str:
    """召回当前用户 + 当前会话的记忆，拼成一个文本块。

    顺序（spec §7）：会话级在前（工作上下文，最高优先），用户级在后。
    全空 → 返回 ""（调用方据此跳过注入，零开销）。
    注：本函数自身不吞异常（保持纯逻辑可测）。若调用方要求「记忆失败绝不影响主答」，须自行 try/except —— Task 7 的 router 调用点已这样包裹。
    """
    parts: list[str] = []

    sm_res = await db.execute(
        select(QASessionMemory).where(QASessionMemory.session_id == session_id)
    )
    sm = sm_res.scalars().one_or_none()
    if sm is not None and (sm.working_summary or "").strip():
        session_block = "【本次会话工作状态】\n" + sm.working_summary.strip()
        focus = sm.focus_entity_ids
        if isinstance(focus, list):
            ids = [x for x in focus if isinstance(x, str) and x]
            if ids:
                session_block += "\n【本次聚焦实体】" + ", ".join(ids)
        parts.append(session_block)

    um_res = await db.execute(
        select(QAUserMemory)
        .where(QAUserMemory.user_id == user_id, QAUserMemory.status == "active")
        .order_by(QAUserMemory.created_at)
    )
    user_rows = um_res.scalars().all()
    if user_rows:
        lines = "\n".join(
            f"- {r.content}" for r in user_rows if (r.content or "").strip()
        )
        if lines:
            parts.append("【用户偏好 / 已知事实】\n" + lines)

    return "\n\n".join(parts)


async def write_explicit_memory(
    db: Any, *, user_id: int, session_id: str, content: str
) -> None:
    """落一条用户级显式记忆（P1：显式只进用户级）。
    注：自身不吞异常（同 recall_memory_block 契约）；Task 7 调用点已 try/except。
    """
    db.add(
        QAUserMemory(
            user_id=user_id,
            kind="preference",
            content=content,
            source="explicit",
            source_session_id=session_id,
            status="active",
        )
    )
    await db.commit()


_FOCUS_MAX = 10  # 聚焦实体上限，控 prompt 体积


def _extract_focus_entity_ids(messages: Any) -> list[str]:
    """从一组消息的 msg_metadata 聚合本次会话聚焦的 entity_id。

    来源：assistant 消息 msg_metadata 里的 cited_entities + entry_points
    （回答时已落库，复用零额外 LLM 成本）。按首见序去重，截 _FOCUS_MAX。
    全程防御：缺属性 / None / 非 dict / 字段非 list / 非字符串项 一律安全跳过。
    设计：[[记忆系统-设计]] §17。
    """
    seen: set[str] = set()
    out: list[str] = []
    for m in messages:
        meta = getattr(m, "msg_metadata", None)
        if not isinstance(meta, dict):
            continue
        for key in ("cited_entities", "entry_points"):
            vals = meta.get(key)
            if not isinstance(vals, list):
                continue
            for v in vals:
                if isinstance(v, str) and v and v not in seen:
                    seen.add(v)
                    out.append(v)
                    if len(out) >= _FOCUS_MAX:
                        return out
    return out


async def maybe_compact_session(
    db: Any, llm: Any, *, session_id: str, every_n_messages: int = 6,
    force: bool = False,
) -> None:
    """会话级压缩：每「自上次压缩以来新增 ≥ every_n_messages 条消息」压缩一次。

    设计：[[记忆系统-设计]] §4.3（P1 固定 N 轮，N=6 条≈3 轮问答）。
    turn_count 记录上次压缩时的 message_count；用「增量 ≥ N」判定，
    而非「过阈值后每轮都压」——否则消息每轮 +2，过阈后每轮都会调 LLM（成本 bug）。
    任何异常都吞掉并 debug 记录（记忆是辅助，绝不影响主答）。
    force=True（上下文压力，spec §18）：越过固定 N floor，但仍要求自上次压缩起 ≥1 条新增。
    """
    try:
        msg_res = await db.execute(
            select(QAMessage)
            .where(QAMessage.session_id == session_id)
            .order_by(QAMessage.created_at)
        )
        messages = msg_res.scalars().all()
        msg_count = len(messages)
        floor = 2 if force else every_n_messages
        if msg_count < floor:
            return

        sm_res = await db.execute(
            select(QASessionMemory).where(QASessionMemory.session_id == session_id)
        )
        sm = sm_res.scalars().one_or_none()

        # 距上次压缩的新增量不足 N → 跳过（实现「每 N 条压一次」而非「过阈后每轮压」）
        prev = (sm.turn_count or 0) if sm is not None else 0
        min_delta = 1 if force else every_n_messages
        if msg_count - prev < min_delta:
            return

        convo = "\n".join(
            f"[{m.role}] {(m.content or '')[:200]}" for m in messages[-12:]
        )
        summary = await llm.complete(system=_SESSION_COMPACT_SYSTEM, user=convo)
        summary = (summary or "").strip()
        if not summary:
            return

        focus = _extract_focus_entity_ids(messages[-12:])

        if sm is None:
            db.add(
                QASessionMemory(
                    session_id=session_id,
                    working_summary=summary,
                    turn_count=msg_count,
                    focus_entity_ids=focus,
                )
            )
        else:
            sm.working_summary = summary
            sm.turn_count = msg_count
            sm.focus_entity_ids = focus
        await db.commit()
    except Exception:
        # 压缩失败绝不影响主流程（spec §4.3）；debug 留痕便于排查（不影响主答）
        _log.debug(
            "maybe_compact_session failed for session %s, silently ignored",
            session_id, exc_info=True,
        )
        return
