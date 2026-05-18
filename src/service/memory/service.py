"""记忆系统 P1 核心逻辑：召回 / 显式意图检测 / 写入 / 会话压缩。

设计：[[记忆系统-设计]] §4.1 §4.3 §6 §7。
纯逻辑，不依赖 FastAPI；DB 用 duck-typed AsyncSession（真跑用 SQLAlchemy，
单测用 Fake），LLM 用 duck-typed provider（有 async complete(system,user)）。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select, or_

from src.service.db_models_homepage import QAUserMemory, QASessionMemory, QAMessage, QAProjectMemory
from src.service.qa_engine.prompts import _SESSION_COMPACT_SYSTEM, _USER_MEM_INTENT_SYSTEM

_log = logging.getLogger(__name__)


# 显式记忆触发词（关键词起步；spec §15 开放问题留 P1 用关键词）
# 命中后剥掉触发词 + 紧随的冒号/空白，剩余即记忆内容。
# ⚠️ 顺序约定：若将来新增触发词是已有词的「超串」（如「顺便记一下」含「记一下」），
#    必须把更具体的放前面，否则被较短前缀先匹配（当前 5 个互不为前缀，安全）。
_TRIGGERS = ("请记住", "记住", "记一下", "记下", "帮我记住")


# 句尾后缀判定前先剥掉的尾部空白与中英文标点（不影响前缀分支）。含中英文分号。
_TRAILING_PUNCT = " 　\t。，、！？.!?,；;"
# 内容右侧清理集（冒号分隔约定残留 + 尾部标点）；DRY：前缀/后缀两分支复用。
_CONTENT_RSTRIP = " :：\t，、," + _TRAILING_PUNCT
# endswith 匹配必须最长触发词优先：「记住」是「请记住」「帮我记住」的后缀，
# 按 _TRIGGERS 原序匹配会把「帮我记住」误剥成「帮我」+「记住」。
# startswith 分支不受影响（5 个触发词互不为前缀，原序安全）。
_TRIGGERS_BY_LEN = tuple(sorted(_TRIGGERS, key=len, reverse=True))


def detect_explicit_memory(question: str) -> str | None:
    """从用户问题里检测显式记忆意图（§22.3：句首前缀 或 句尾后缀）。

    前缀命中：content = 触发词之后；再剥 rest 末尾多余触发词（如「记住A 请记住」→「A」），
    但内容恰为触发词本身时不剥空（len 守卫，保旧行为「请记住记住」→「记住」）。
    句尾后缀命中：q 去右侧空白与中英文标点后 endswith(触发词) → content = 其之前部分。
    endswith 匹配按触发词长度降序（_TRIGGERS_BY_LEN），避免「帮我记住」被「记住」抢匹配。
    两侧 content 再清理；空 → None。前缀优先（都命中按前缀）。
    未命中 → None。"任意位置包含"不算（"我不需要你记住" 不误判）。
    """
    q = (question or "").strip()
    if not q:
        return None
    for trig in _TRIGGERS:
        if q.startswith(trig):
            rest = q[len(trig):].lstrip(" :：\t").strip()
            # 前缀命中后剥掉 rest 自身末尾多余的触发词（如「记住A 请记住」→「A」）；
            # 内容恰为触发词本身（len 不大于触发词）则不剥（「请记住记住」→「记住」）。
            rest_tail = rest.rstrip(_TRAILING_PUNCT)
            for t2 in _TRIGGERS_BY_LEN:
                if rest_tail.endswith(t2) and len(rest_tail) > len(t2):
                    rest = rest_tail[: -len(t2)].rstrip(_CONTENT_RSTRIP).strip()
                    break
            else:
                rest = rest_tail.strip()
            return rest or None
    q_tail = q.rstrip(_TRAILING_PUNCT)
    for trig in _TRIGGERS_BY_LEN:
        if q_tail.endswith(trig) and len(q_tail) > len(trig):
            rest = q_tail[: -len(trig)].rstrip(_CONTENT_RSTRIP).strip()
            return rest or None
    return None


# 工程级显式触发词。不含冒号——靠 lstrip 容错「：/:/空格」分隔。
# ⚠️ 这些是「记住」的超串，调用方（_make_memory_writer）必须先调本检测器、
#    后调通用 detect_explicit_memory，否则「记住这个工程：X」会被误判为 user 级。
_PROJECT_TRIGGERS = ("记住这个工程", "记住本工程", "记住该工程", "工程记住")
_PROJECT_MEMORY_LIMIT = 20    # 工程记忆 S1 单次 recall 上限（spec §19）


def detect_explicit_project_memory(question: str) -> str | None:
    """检测工程级显式记忆意图（「记住这个工程：…」）。

    命中 → 剥前缀 + 起始冒号/空白，返回内容；未命中/空 → None。

    ⚠️ 调用契约：必须先于 detect_explicit_memory 调用。三个触发词
    「记住这个工程/记住本工程/记住该工程」都是「记住」的超串——若先调通用
    detect_explicit_memory，会把工程输入误判为 user 级写入（内容还带「这个工程：」垃圾前缀）。
    """
    q = (question or "").strip()
    for trig in _PROJECT_TRIGGERS:
        if q.startswith(trig):
            rest = q[len(trig):].lstrip(" :：\t").strip()
            return rest or None
    return None


async def recall_memory_block(db: Any, *, user_id: int, session_id: str, project_id: str | None = None) -> str:
    """召回当前用户 + 当前会话 + 工程的记忆，拼成一个文本块。

    顺序（spec §7）：会话级在前（工作上下文，最高优先），用户级次之，工程级末位。
    project_id=None 不出工程块（向后兼容）。
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

    if project_id is not None:
        pm_res = await db.execute(
            select(QAProjectMemory)
            .where(
                QAProjectMemory.project_id == project_id,
                QAProjectMemory.status == "active",
                or_(
                    QAProjectMemory.scope == "team",
                    QAProjectMemory.user_id == user_id,
                ),
            )
            .order_by(QAProjectMemory.created_at)
            .limit(_PROJECT_MEMORY_LIMIT)
        )
        proj_rows = pm_res.scalars().all()
        if proj_rows:
            lines = "\n".join(
                f"- {r.content}" for r in proj_rows if (r.content or "").strip()
            )
            if lines:
                parts.append("【工程记忆】\n" + lines)

    return "\n\n".join(parts)


_VALID_KINDS = ("identity", "preference", "style_feedback")


async def parse_user_memory_intent(llm: Any, content: str) -> dict:
    """轻量 LLM 解析显式记忆意图（§22.4）。返回
    {tier:'user'|'skip', kind:'identity'|'preference'|'style_feedback',
     content:str, supersedes_kind:'identity'|None}。
    任何异常/非法 JSON/字段非法 → 兜底 {user, preference, 原 content, None}（绝不丢、绝不抛）。
    """
    fallback = {
        "tier": "user", "kind": "preference",
        "content": content, "supersedes_kind": None,
    }
    try:
        raw = await llm.complete(system=_USER_MEM_INTENT_SYSTEM, user=content)
    except Exception:
        return fallback
    s = (raw or "").strip()
    if s.startswith("```"):
        # 去 ```json ... ``` 围栏
        s = s.split("```")[1] if "```" in s[3:] else s.lstrip("`")
        if s.startswith("json"):
            s = s[4:]
        s = s.strip().strip("`").strip()
    try:
        obj = json.loads(s)
    except Exception:
        return fallback
    if not isinstance(obj, dict):
        return fallback
    tier = obj.get("tier")
    kind = obj.get("kind")
    c = obj.get("content")
    sk = obj.get("supersedes_kind")
    if tier not in ("user", "skip"):
        return fallback
    if kind not in _VALID_KINDS:
        return fallback
    if not isinstance(c, str) or not c.strip():
        return fallback
    if sk not in ("identity", None):
        sk = None
    return {"tier": tier, "kind": kind, "content": c.strip(), "supersedes_kind": sk}


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


async def write_explicit_project_memory(
    db: Any, *, project_id: str, user_id: int, session_id: str, content: str
) -> None:
    """落一条工程级显式记忆（S1：scope=private，source=explicit，status=active）。
    注：自身不吞异常（同 recall/user-write 契约）；调用点 try/except。
    """
    db.add(
        QAProjectMemory(
            project_id=project_id,
            user_id=user_id,
            scope="private",
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

        # §21 递归累积：输入 = 上一版摘要 + 仅自水位线(prev)以来的新增消息。
        # 早期事实进早期摘要后被永久滚动保留（对齐 Claude Code 携带摘要再摘要）；
        # 输入恒有界（旧摘要 ≤ 摘要上限 + 新增量有界），无 token 膨胀。
        prev_summary = (sm.working_summary or "").strip() if sm is not None else ""
        new_msgs = messages[prev:]
        parts: list[str] = []
        if prev_summary:
            parts.append("【已有会话摘要】\n" + prev_summary)
        # 守卫已保证 msg_count - prev >= min_delta >= 1 → new_msgs 必非空；
        # 仍以 if 守一层，与 prev_summary 段对称且对未来阈值改动稳健（记忆辅助路径绝不抛）。
        if new_msgs:
            parts.append(
                "【新增对话】\n"
                + "\n".join(
                    f"[{m.role}] {(m.content or '')[:200]}" for m in new_msgs
                )
            )
        convo = "\n\n".join(parts)
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
