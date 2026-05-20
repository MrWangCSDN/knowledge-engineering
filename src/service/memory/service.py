"""记忆系统 P1 核心逻辑：召回 / 显式意图检测 / 写入 / 会话压缩。

设计：[[记忆系统-设计]] §4.1 §4.3 §6 §7。
纯逻辑，不依赖 FastAPI；DB 用 duck-typed AsyncSession（真跑用 SQLAlchemy，
单测用 Fake），LLM 用 duck-typed provider（有 async complete(system,user)）。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy import select

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


async def recall_memory_block(
    fs,                                          # 类型由 import 决定（避免循环导入 → 不在签名标注 MemoryFS）
    query: str,
    user_id: int,
    *,
    top_k: int = 5,
) -> str:
    """召回当前用户的记忆 L0 拼装为 system-prompt 块（S3 实现）。

    新签名（drop-in 替换 §22 DB 版）：fs + query + user_id；返回 str（空字符串
    表示无记忆，调用方 with_memory_block 据此跳过注入零开销）。
    内部委托 MemoryRecaller；S3 自包失败语义（embed/query 失败返 ""），
    调用方 try/except 不再必需（但 qa_router 既有 try 保留为防御性深度防护）。

    设计：[[文件式记忆重构-设计]] §4.1（drop-in 替换契约）+ §4.4（召回算法）。
    """
    # 局部 import：避免模块顶层循环引入（recall.py → memgen.py → 不回环 service.py）
    from src.service.memory.recall import MemoryRecaller, MemoryL0Store, _DefaultEmbedder
    # 单例 / 注入：本函数走 KE 部署期既定 Weaviate 配置；在生产里由 KE 框架装配
    # 注入；此处为简化集成层调用，按 KE 既有 Weaviate 客户端创建惯例就地构造。
    # 注：MemoryL0Store(BaseWeaviateStore) 的 __init__ 同步打开连接 / 检查 schema，
    # 重复构造代价不大（KE 既有 weaviate_*_store.py 同模式）；如需性能优化（连接池），
    # 由 S7 / 独立 hardening pass 引入单例（不在 S3 MVP）。
    # 为可测：本函数若被现存测试以 fake fs 调入但无 Weaviate 部署，会因
    # MemoryL0Store 构造抛 ConnectionError → 进入 S3 自包 try 路径返 ""。
    try:
        # 读 KE 既有部署配置（与 KE 既有 weaviate stores 一致的入口）
        import os
        url = os.getenv("WEAVIATE_URL", "http://127.0.0.1:8080")
        grpc_port = int(os.getenv("WEAVIATE_GRPC_PORT", "50051"))
        api_key = os.getenv("WEAVIATE_API_KEY") or None
        # 构造 MemoryL0Store（同步初始化连接 + 必要时建 collection）
        store = MemoryL0Store(
            url=url,
            grpc_port=grpc_port,
            collection_name="memory_l0",
            dimension=1024,
            api_key=api_key,
        )
        # MemoryL0Store._client 是 weaviate v4 client；MemoryRecaller 直接接收 client
        recaller = MemoryRecaller(embedder=_DefaultEmbedder(), weaviate_client=store._client)
        return await recaller.recall_memory_block(fs, query, user_id, top_k=top_k)
    except Exception as exc:
        # S3 自包失败语义：任何异常 → 返 ""（with_memory_block 走零开销不注入路径）。
        # 加 debug log 让运维在 Weaviate 不可达 / env 解析失败时有迹可循（修 T4 review I1）；
        # MemoryRecaller 内部各步已自记 debug log，本层覆盖 wrapper 构造期的 ConnectionError 等。
        _log.debug("recall_memory_block wrapper failed: %r", exc)
        return ""


# 去 LLM 输出的 ```json … ``` / ``` … ``` / ````json … ```` 围栏（1~4 反引号、可选 json 标签）
_CODE_FENCE_RE = re.compile(r"^`{1,4}(?:json)?\s*(.*?)\s*`{1,4}$", re.DOTALL)


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
    _m = _CODE_FENCE_RE.match(s)
    if _m:
        s = _m.group(1).strip()
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
    db: Any, *, user_id: int, session_id: str, content: str,
    kind: str = "preference", supersedes_kind: str | None = None,
) -> None:
    """落一条用户级显式记忆（§22.5）。

    kind=='identity' 或 supersedes_kind=='identity'：先把该 user 所有
    kind='identity' AND status='active' 行 status→'archived'（软删，宪法禁永久删），
    再 INSERT 新行 —— identity 单例，新名字取代旧名字。
    kind in ('preference','style_feedback')：仅追加（多条并存）。
    归档+插入同一次 commit。kind 默认 'preference'（旧调用零改动，不破坏）。

    入参契约：supersedes_kind=='identity' 的 OR 分支是前向兼容位；当前 §22.4 解析器
    对身份事实总是同时置 kind='identity' 与 supersedes_kind='identity'。调用方若要触发
    身份取代，**必须**令 kind='identity'（勿用 kind='preference'+supersedes_kind='identity'，
    那会归档旧 identity 却插入一条 preference，导致 0 条 active identity）。
    并发：本函数在 post-turn 单会话后台回调内调用，同 user 并发写竞争窗口可忽略，
    不加锁（记忆为辅助路径，瞬时重复下一次身份写自愈）。
    自身不吞异常（同 recall_memory_block 契约）；router 调用点已 try/except。
    """
    if kind == "identity" or supersedes_kind == "identity":
        res = await db.execute(
            select(QAUserMemory).where(
                QAUserMemory.user_id == user_id,
                QAUserMemory.kind == "identity",
                QAUserMemory.status == "active",
            )
        )
        for r in res.scalars().all():
            # guard 仅对测试 _FakeMemDB 生效（它不解析/套用 WHERE，返回全部 user_rows）；
            # 真实 ORM 行 kind/status 是 NOT NULL 映射列、且已被上面的 WHERE 限定，
            # 不需要 getattr。WHERE 子句的回归由 test_write_identity_select_has_where_filter 锁定。
            if getattr(r, "kind", None) == "identity" and getattr(r, "status", None) == "active":
                r.status = "archived"
    db.add(
        QAUserMemory(
            user_id=user_id,
            kind=kind,
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
