"""记忆系统 P1 核心逻辑：召回 / 显式意图检测 / 写入 / 会话压缩。

设计：[[记忆系统-设计]] §4.1 §4.3 §6 §7。
纯逻辑，不依赖 FastAPI；DB 用 duck-typed AsyncSession（真跑用 SQLAlchemy，
单测用 Fake），LLM 用 duck-typed provider（有 async complete(system,user)）。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from src.service.db_models_homepage import QAUserMemory, QASessionMemory, QAMessage
from src.service.qa_engine.prompts import _SESSION_COMPACT_SYSTEM


# 显式记忆触发词（关键词起步；spec §15 开放问题留 P1 用关键词）
# 命中后剥掉触发词 + 紧随的冒号/空白，剩余即记忆内容。
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
    """
    parts: list[str] = []

    sm_res = await db.execute(
        select(QASessionMemory).where(QASessionMemory.session_id == session_id)
    )
    sm = sm_res.scalars().one_or_none()
    if sm is not None and (sm.working_summary or "").strip():
        parts.append("【本次会话工作状态】\n" + sm.working_summary.strip())

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
    """落一条用户级显式记忆（P1：显式只进用户级）。"""
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


async def maybe_compact_session(
    db: Any, llm: Any, *, session_id: str, every_n_messages: int = 6
) -> None:
    """会话级压缩：消息数达到 every_n_messages 且自上次压缩后有增长时，
    把该会话全部消息压成一段「工作状态」，覆盖式 upsert qa_session_memory。

    设计：[[记忆系统-设计]] §4.3（P1 固定 N 轮，N=6 条≈3 轮问答）。
    任何异常都吞掉（记忆是辅助）。
    """
    try:
        msg_res = await db.execute(
            select(QAMessage)
            .where(QAMessage.session_id == session_id)
            .order_by(QAMessage.created_at)
        )
        messages = msg_res.scalars().all()
        msg_count = len(messages)
        if msg_count < every_n_messages:
            return

        sm_res = await db.execute(
            select(QASessionMemory).where(QASessionMemory.session_id == session_id)
        )
        sm = sm_res.scalars().one_or_none()

        if sm is not None and msg_count <= (sm.turn_count or 0):
            return

        convo = "\n".join(
            f"[{m.role}] {(m.content or '')[:200]}" for m in messages[-12:]
        )
        summary = await llm.complete(system=_SESSION_COMPACT_SYSTEM, user=convo)
        summary = (summary or "").strip()
        if not summary:
            return

        if sm is None:
            db.add(
                QASessionMemory(
                    session_id=session_id,
                    working_summary=summary,
                    turn_count=msg_count,
                )
            )
        else:
            sm.working_summary = summary
            sm.turn_count = msg_count
        await db.commit()
    except Exception:
        return
