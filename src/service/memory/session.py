"""文件式记忆 S5：会话级 working_summary 文件化压缩器 + 读侧 composer。

设计：[[文件式记忆重构-设计]] §6（§6.0–§6.9）。纯逻辑，不依赖 FastAPI；
LLM 用 duck-typed provider（鸭子 async complete(system,user,**kw)->str）。

S5 公开 API（§6.2）：
- ``SessionCompactor(llm).compact(fs, *, user_id, session_id, ...)`` — 写侧
- ``read_session_summary(fs, *, user_id, session_id) -> str`` — 读侧
- ``_summary_uri(user_id, session_id) -> str`` — URI 派生 helper

S6 新增 API（§7.3）：
- ``write_message_to_fs(fs, *, user_id, session_id, msg_id, role, content, ...)`` — message 写侧
- ``read_messages_for_session(fs, *, user_id, session_id) -> list[_FsMessage]`` — message 读侧

`maybe_compact_session` (service.py) 的 1:1 语义替换：算法与守卫 verbatim 保留
（§6.4 数据流），仅把 DB↔fs 交换。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from src.service.memory.vfs import MemoryFS, MemoryNotFound
from src.service.memory.memgen import _render_frontmatter, _split_frontmatter
from src.service.memory.service import _extract_focus_entity_ids
from src.service.memory.extract import _now_iso_z          # S4 既有 helper，原样复用
from src.service.qa_engine.prompts import _SESSION_COMPACT_SYSTEM

# 模块级 logger（与 vfs.py / memgen.py / recall.py / extract.py 同模式）
_log = logging.getLogger(__name__)


def _summary_uri(user_id: int, session_id: str) -> str:
    """URI 派生 helper：ke://u/{uid}/session/{sid}/summary.md（§6.2 唯一路径形态）。

    本函数纯字符串拼装，不调 fs.resolve（safe 校验留给 fs.read/fs.write）；
    user_id 是 KE Integer（≥1，DB 自增），session_id 是 KE String(64)（业务串）。
    """
    # f-string 拼 ke:// 前缀 + /u/{uid}/session/{sid}/summary.md
    return f"ke://u/{user_id}/session/{session_id}/summary.md"


async def read_session_summary(
    fs: MemoryFS, *, user_id: int, session_id: str,
) -> str:
    """读 session summary.md 的 body 段（去 frontmatter）。

    设计：[[文件式记忆重构-设计]] §6.4 读侧算法。
    不存在 / 失败 → 返 ""（与 recall_memory_block 同自包失败语义，§6.5）。
    composer 直接拼到 memory_block 头部（qa_router 5b/5c 段，T4 接入）。

    frontmatter 损坏（YAML 非法）时 `_split_frontmatter` 容错返 ({}, body)，
    本函数仍返 body 作 summary；frontmatter 闭合未探到时返 ({}, 原文)，
    本函数返裸文本（自愈优先，§6.5）。
    """
    try:
        # 拼路径（§6.2 唯一形态）
        uri = _summary_uri(user_id, session_id)
        # fs.read 不存在抛 MemoryNotFound（vfs.py read 契约）
        raw = await fs.read(uri)
        # S2 helper 拆 frontmatter；非法 YAML 已被 S2 内部容错为空 dict
        # body 是闭合后的部分（或无 frontmatter 时即全文）
        _meta, body = _split_frontmatter(raw)
        # strip 去尾换行（_render_frontmatter 写时带 "\n"，读时去尾保持纯文本）
        return (body or "").strip()
    except MemoryNotFound:
        # 首次会话尚无 summary.md 是正常路径，非异常；返 "" 让 composer 跳注入
        return ""
    except Exception as exc:                              # 任何其他异常深度兜底
        # 中层失败语义（§6.5）：debug 留痕 + 返 ""，不抛
        _log.debug("read_session_summary failed: %r", exc)
        return ""


class SessionCompactor:
    """会话级摘要文件化压缩器（替代 maybe_compact_session）。

    设计：[[文件式记忆重构-设计]] §6（§6.2/§6.4）。同 S4 MemoryExtractor 架构：
    ``__init__`` 仅绑 llm，fs/db 走方法形参（便于测试 fake，便于 S7 singletonize）。

    单例策略：S7 把 SessionCompactor + MemoryExtractor 一同提到 module-level
    singleton；S5 仍在闭包内构造一次（成本可忽略；与 S4 同模式）。
    """

    def __init__(self, llm) -> None:
        """绑 LLM provider（鸭子 ``async complete(system,user,**kw)->str``）。"""
        # 仅持 llm；fs/db 走 compact() 形参（§6.2 接口契约）
        self._llm = llm

    async def compact(
        self,
        fs: MemoryFS,
        *,
        user_id: int,
        session_id: str,
        every_n_messages: int = 6,
        force: bool = False,
    ) -> None:
        """post-turn 触发的会话压缩。

        设计：[[文件式记忆重构-设计]] §7.5（S6：删 db 参数，step 1 改 fs.ls）。
        保留 §6.4 verbatim 8 步算法 + 守卫语义；仅替换 DB↔fs 数据源。

        S6 后 backward-incompatible 签名变更：删 `db` 参数（step 1 不再读 DB）。
        调用方（qa_router._make_memory_writer 闭包）同步改 `compactor.compact(fs, user_id=..., session_id=..., force=...)`。

        Args:
            fs: S1 文件存储层（duck-type async API）— 既读 messages 也写 summary
            user_id: KE Integer ≥1，租户 ID
            session_id: KE String(64)，业务会话串
            every_n_messages: floor 阈值；msg_count < N 时早退（§6.4 step 2）
            force: True 表示上下文压力触发（spec §18），降低 floor 至 2、min_delta 至 1
        """
        try:
            # ─── step 1: 拉本 session 全部 messages（S6 后：fs 而非 DB） ───
            # read_messages_for_session 按 created_at 升序返；目录不存在返 []
            messages = await read_messages_for_session(
                fs, user_id=user_id, session_id=session_id
            )
            msg_count = len(messages)

            # ─── step 2: floor 判定（与 S5 同：force 降门槛到 2） ───
            floor = 2 if force else every_n_messages
            if msg_count < floor:
                return

            # ─── step 3: 读旧 summary.md（不存在 → 首压） ───
            uri = _summary_uri(user_id, session_id)
            prev_turn_count = 0
            prev_summary = ""
            try:
                raw = await fs.read(uri)
                fm, body = _split_frontmatter(raw)
                prev_summary = (body or "").strip()
                tc = fm.get("turn_count")
                if isinstance(tc, int) and tc >= 0:
                    prev_turn_count = tc
            except MemoryNotFound:
                pass

            # ─── step 4: delta 判定 ───
            min_delta = 1 if force else every_n_messages
            if msg_count - prev_turn_count < min_delta:
                return

            # ─── step 5: 拼 convo（§21 递归累积） ───
            new_msgs = messages[prev_turn_count:]
            parts: list[str] = []
            if prev_summary:
                parts.append("【已有会话摘要】\n" + prev_summary)
            if new_msgs:
                parts.append(
                    "【新增对话】\n"
                    + "\n".join(
                        f"[{m.role}] {(m.content or '')[:200]}" for m in new_msgs
                    )
                )
            convo = "\n\n".join(parts)

            # ─── step 6: LLM 调用 ───
            summary = await self._llm.complete(system=_SESSION_COMPACT_SYSTEM, user=convo)
            summary = (summary or "").strip()
            if not summary:
                return

            # ─── step 7: focus_entity_ids 聚合 ───
            focus = _extract_focus_entity_ids(messages[-12:])

            # ─── step 8: 写新 summary.md ───
            fm_new = {
                "turn_count": msg_count,
                "focus_entity_ids": focus,
                "updated_at": _now_iso_z(),
            }
            content = _render_frontmatter(fm_new, summary + "\n")
            await fs.write(uri, content)

        except Exception:
            _log.debug(
                "SessionCompactor.compact failed for session %s, silently ignored",
                session_id, exc_info=True,
            )
            return


# ─── S6: fs-back message helpers（§7.3）──────────────────────────────────────


@dataclass
class _FsMessage:
    """fs-back message 鸭子类型（duck-type 等价于已删的 QAMessage ORM）。

    设计：[[文件式记忆重构-设计]] §7.3。
    满足 SessionCompactor.compact step 5 + _extract_focus_entity_ids 的属性访问契约：
    - role / content / msg_metadata / created_at / sections

    S6 后 `SessionCompactor.compact step 1` 读 fs 返 `list[_FsMessage]` 替代
    DB ORM rows；下游代码（step 5 / step 7）按属性访问对接 — 鸭子兼容。
    """
    role: str
    content: str | None
    msg_metadata: dict | None
    created_at: datetime
    sections: list | None = None


def _messages_dir_uri(user_id: int, session_id: str) -> str:
    """URI 派生：ke://u/{uid}/session/{sid}/messages（无尾 /）。"""
    # 与 _summary_uri 同模式；末段不含文件名 — 用于 fs.ls 目录扫描
    return f"ke://u/{user_id}/session/{session_id}/messages"


def _message_uri(user_id: int, session_id: str, msg_id: str) -> str:
    """URI 派生：ke://u/{uid}/session/{sid}/messages/{msg_id}.md。

    msg_id 复用 KE 既有 String(64)（如 "msg_xyz789"），跨 session 唯一。
    """
    return f"ke://u/{user_id}/session/{session_id}/messages/{msg_id}.md"


async def write_message_to_fs(
    fs: MemoryFS,
    *,
    user_id: int,
    session_id: str,
    msg_id: str,
    role: str,
    content: str | None,
    sections: list[dict] | None = None,
    msg_metadata: dict | None = None,
    created_at: datetime | None = None,
) -> None:
    """写一条 message 到 fs（per-message file）。

    设计：[[文件式记忆重构-设计]] §7.3 + §7.4。
    `qa_router persist_messages` callback 用此 helper 替换 DB add/commit。

    路径：ke://u/{uid}/session/{sid}/messages/{msg_id}.md
    frontmatter: role + created_at(ISO Z) + 可选 sections + 可选 msg_metadata
    body: content + "\\n"（与 S5 SessionCompactor.compact step 8 同约定）

    Args:
        fs: S1 文件存储层
        user_id / session_id / msg_id: 路径派生
        role: "user" / "assistant"
        content: 文本内容（user 必有；assistant 可为 None — 6 段式在 sections）
        sections: assistant 才有的 6 段式结构化内容
        msg_metadata: assistant 才有的 entry_points / cited_entities / token_usage 等
        created_at: 不传则用当前 UTC 时间（datetime.now(timezone.utc)）
    """
    # 时区归一：null 默认 UTC now；naive 视作 UTC；非 UTC tz-aware 转 UTC
    # 保证 frontmatter.created_at 始终是合法 ISO 8601 Z 字符串
    if created_at is None:
        created_at = datetime.now(timezone.utc)
    elif created_at.tzinfo is None:
        # naive 视作 UTC（与 S4 _now_iso_z 同模式：UTC 为默认时区）
        created_at = created_at.replace(tzinfo=timezone.utc)
    else:
        # 非 UTC tz-aware → 归一为 UTC（避免错标 Z）
        created_at = created_at.astimezone(timezone.utc)
    # 现在 created_at 必是 UTC-aware
    fm: dict = {
        "role": role,
        # ISO 8601 Z 字符串（与 S4 _now_iso_z 同格式）
        "created_at": created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if sections is not None:
        fm["sections"] = sections
    if msg_metadata is not None:
        fm["msg_metadata"] = msg_metadata
    # body 末尾换行（S2 _render_frontmatter docstring 约定）
    body = (content or "") + "\n"
    raw = _render_frontmatter(fm, body)
    uri = _message_uri(user_id, session_id, msg_id)
    await fs.write(uri, raw)


async def read_messages_for_session(
    fs: MemoryFS, *, user_id: int, session_id: str,
) -> list[_FsMessage]:
    """读 session 全部 message 文件，按 created_at 升序返。

    设计：[[文件式记忆重构-设计]] §7.3。drop-in 替换 SessionCompactor.compact
    step 1 的 DB `select QAMessage`（S6 后唯一 message 真相源）。

    路径不存在（session 无消息）→ 返 []（首压路径 / 新 session）。
    单 message 文件损坏 → _log.debug 跳过该消息（与 S2 同模式）。
    """
    dir_uri = _messages_dir_uri(user_id, session_id)
    try:
        filenames = await fs.ls(dir_uri)
    except MemoryNotFound:
        # 目录不存在 = session 还没写过任何 message → 返空
        return []
    except Exception as exc:
        _log.debug("read_messages_for_session ls failed: %r", exc)
        return []

    out: list[_FsMessage] = []
    for fname in filenames:
        # S7 加 .feedback.md 过滤：feedback sibling 文件（msg_xyz.feedback.md，§8.4）
        # 文件名也 endswith ".md" 但不是 message — 否则误读为 message 导致 _FsMessage
        # 解析失败（缺 role 字段被 inner skip path 兜底，但走错路径有性能浪费）
        if not fname.endswith(".md") or fname.endswith(".feedback.md"):
            continue
        file_uri = f"{dir_uri}/{fname}"
        try:
            raw = await fs.read(file_uri)
            fm, body = _split_frontmatter(raw)
            role = fm.get("role")
            if not isinstance(role, str) or not role:
                _log.debug("read_messages_for_session: skip missing role in %s", file_uri)
                continue
            created_at_str = fm.get("created_at")
            if not isinstance(created_at_str, str):
                _log.debug("read_messages_for_session: skip missing created_at in %s", file_uri)
                continue
            # Python 3.12 datetime.fromisoformat 原生支持 Z 后缀
            created_at = datetime.fromisoformat(created_at_str)
            msg_metadata = fm.get("msg_metadata")
            if msg_metadata is not None and not isinstance(msg_metadata, dict):
                msg_metadata = None
            sections = fm.get("sections")
            if sections is not None and not isinstance(sections, list):
                sections = None
            content_text = body.strip() if isinstance(body, str) else None
            out.append(_FsMessage(
                role=role,
                content=content_text,
                msg_metadata=msg_metadata,
                created_at=created_at,
                sections=sections,
            ))
        except Exception as exc:
            _log.debug("read_messages_for_session: skip corrupt file %s: %r", file_uri, exc)
            continue

    # 按 created_at 升序排序（不依赖 fs.ls 字典序 — 文件名是 msg_id，无时间排序保证）；
    # 同 created_at 时按 role tie-break（user 在前 assistant 在后），与旧 DB
    # qa_router.py:463 case((QAMessage.role == "user", 0), else_=1) 同语义 —
    # persist_messages 同一调用写 user+assistant 极可能同秒（strftime 截微秒）
    out.sort(key=lambda m: (m.created_at, 0 if m.role == "user" else 1))
    return out


# ─── S7: fs-back feedback helpers（§8.4）─────────────────────────────────────


@dataclass
class _FsFeedback:
    """fs-back feedback 鸭子类型（duck-type 等价于 S6 已删的 QAFeedback ORM）。

    设计：[[文件式记忆重构-设计]] §8.4（S7 修 S6 §7.11 #1 broken stub）。
    一条 assistant message 对应 0 或 1 feedback（覆盖式更新；用户可改投票 / 取消）。

    字段：
    - vote: "up" / "down" / None（取消反馈）
    - user_id: 反馈人 user.id
    - comment: optional 文字反馈
    - created_at: UTC-aware datetime（write 端归一化保证）
    """
    vote: str | None
    user_id: int
    comment: str | None
    created_at: datetime


def _feedback_uri(user_id: int, session_id: str, msg_id: str) -> str:
    """URI 派生：ke://u/{uid}/session/{sid}/messages/{msg_id}.feedback.md（§8.4）。

    与 message 同目录，`.feedback.md` 后缀隐含与 {msg_id}.md 关联；
    delete_session recursive rm（§8.3）时一同被清；
    需配套 read_messages_for_session 加 `.feedback.md` 过滤避误读为 message。
    """
    return f"ke://u/{user_id}/session/{session_id}/messages/{msg_id}.feedback.md"


async def write_feedback_to_fs(
    fs: MemoryFS, *, user_id: int, session_id: str, msg_id: str,
    vote: str | None, comment: str | None = None,
    created_at: datetime | None = None,
) -> None:
    """写一条 feedback 到 fs（覆盖式更新；新 feedback 覆盖旧文件）。

    设计：[[文件式记忆重构-设计]] §8.4。
    qa_router post_feedback endpoint 用此 helper 替换 S6 后的 404 stub。

    Args:
        fs: S1 文件存储层
        user_id / session_id / msg_id: 路径派生
        vote: "up" / "down" / None（取消反馈）— None 序列化为 YAML null
        comment: optional 文字反馈（写入 body）
        created_at: 不传则用当前 UTC；naive datetime 视作 UTC；非 UTC tz-aware 转 UTC
                    （同 S6 write_message_to_fs I1 fix 模式，保证 frontmatter Z 字符串正确）
    """
    # 时区归一为 UTC-aware（同 write_message_to_fs 模式）
    if created_at is None:
        created_at = datetime.now(timezone.utc)
    elif created_at.tzinfo is None:
        # naive 视作 UTC（与 S4 _now_iso_z / S6 I1 fix 同模式）
        created_at = created_at.replace(tzinfo=timezone.utc)
    else:
        # 非 UTC tz-aware → 归一为 UTC（防错标 Z）
        created_at = created_at.astimezone(timezone.utc)

    fm: dict = {
        "vote": vote,            # 显式 None 允许（取消反馈）— YAML 序列化为 null
        "user_id": user_id,
        "created_at": created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    # body 末尾换行（S2 _render_frontmatter 约定）；comment 可能 None
    body = (comment or "") + "\n"
    raw = _render_frontmatter(fm, body)
    uri = _feedback_uri(user_id, session_id, msg_id)
    # fs.write 原子（S1 os.replace POSIX rename）；覆盖式自然达成（同路径 → 替换）
    await fs.write(uri, raw)


async def read_feedback_for_message(
    fs: MemoryFS, *, user_id: int, session_id: str, msg_id: str,
) -> _FsFeedback | None:
    """读单条 message 的 feedback；不存在返 None（首次访问，正常路径）。

    设计：[[文件式记忆重构-设计]] §8.4。
    单文件损坏（缺字段 / fromisoformat 抛）→ debug 返 None（§8.5 中层失败语义）。
    """
    uri = _feedback_uri(user_id, session_id, msg_id)
    try:
        raw = await fs.read(uri)
        fm, body = _split_frontmatter(raw)
        # vote 字段：允许 None / str；其他类型容错为 None
        vote = fm.get("vote")
        if vote is not None and not isinstance(vote, str):
            vote = None
        # user_id 必须 int — 损坏文件直接返 None
        uid_val = fm.get("user_id")
        if not isinstance(uid_val, int):
            return None
        # created_at 必须可解析 ISO — 损坏 / 缺失返 None
        created_at_str = fm.get("created_at")
        if not isinstance(created_at_str, str):
            return None
        created_at = datetime.fromisoformat(created_at_str)
        # body strip 后为 "" 时视作无 comment（None）；非 "" 才入 comment
        comment = body.strip() if isinstance(body, str) and body.strip() else None
        return _FsFeedback(
            vote=vote,
            user_id=uid_val,
            comment=comment,
            created_at=created_at,
        )
    except MemoryNotFound:
        # 该 message 还没 feedback（首次 GET / 用户未投票）— 正常路径
        return None
    except Exception:
        # 其他异常（fromisoformat ValueError / fs 权限 / 损坏 YAML 等）— 中层兜底
        # exc_info=True：保留 traceback 便于生产环境诊断损坏的 feedback 文件
        _log.debug("read_feedback_for_message failed", exc_info=True)
        return None
