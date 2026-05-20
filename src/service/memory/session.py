"""文件式记忆 S5：会话级 working_summary 文件化压缩器 + 读侧 composer。

设计：[[文件式记忆重构-设计]] §6（§6.0–§6.9）。纯逻辑，不依赖 FastAPI；
DB 用 duck-typed AsyncSession（真跑用 SQLAlchemy，单测用 Fake），
LLM 用 duck-typed provider（鸭子 async complete(system,user,**kw)->str）。

S5 公开 API（§6.2）：
- ``SessionCompactor(llm).compact(fs, db, *, user_id, session_id, ...)`` — 写侧
- ``read_session_summary(fs, *, user_id, session_id) -> str`` — 读侧
- ``_summary_uri(user_id, session_id) -> str`` — URI 派生 helper

`maybe_compact_session` (service.py) 的 1:1 语义替换：算法与守卫 verbatim 保留
（§6.4 数据流），仅把 DB↔fs 交换。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from src.service.db_models_homepage import QAMessage
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
        db: Any,
        *,
        user_id: int,
        session_id: str,
        every_n_messages: int = 6,
        force: bool = False,
    ) -> None:
        """post-turn 触发的会话压缩。算法 §6.4（T2 实现完整体）。

        Args:
            fs: S1 文件存储层（duck-type async API）
            db: SQLAlchemy AsyncSession（duck-type；S5 仅读 QAMessage，S6 才迁文件）
            user_id: KE Integer ≥1，租户 ID
            session_id: KE String(64)，业务会话串
            every_n_messages: floor 阈值；msg_count < N 时早退（§6.4 step 2）
            force: True 表示上下文压力触发（spec §18），降低 floor 至 2、min_delta 至 1
        """
        try:
            # ─── step 1: 拉本 session 所有 messages（仍 DB；QAMessage 文件化属 S6） ───
            # select(QAMessage).where(...).order_by(...) 沿用 service.py 既有写法
            msg_res = await db.execute(
                select(QAMessage)
                .where(QAMessage.session_id == session_id)
                .order_by(QAMessage.created_at)
            )
            # .scalars().all() 把 Row 对象拆为列扁平 list（ORM 单实体 select 标准用法）
            messages = msg_res.scalars().all()
            msg_count = len(messages)

            # ─── step 2: floor 判定（与旧版 maybe_compact_session 同：force 降门槛到 2） ───
            # force=True（上下文压力 spec §18）：越过固定 N floor，但仍要求 msg_count ≥ 2
            floor = 2 if force else every_n_messages
            if msg_count < floor:
                # 消息数不足 floor → 不调 LLM、不读 fs、不写 fs（成本守卫）
                return

            # ─── step 3: 读旧 summary.md（不存在 → 首压，prev_turn_count=0, prev_summary="") ───
            uri = _summary_uri(user_id, session_id)
            prev_turn_count = 0
            prev_summary = ""
            try:
                # fs.read 不存在抛 MemoryNotFound（vfs.py read 契约）
                raw = await fs.read(uri)
                # S2 helper 拆 frontmatter；非法 YAML 已被 S2 内部容错为空 dict {}
                fm, body = _split_frontmatter(raw)
                # body 是 frontmatter 闭合后的部分（或无 frontmatter 时即全文）
                prev_summary = (body or "").strip()
                # fm 由 _split_frontmatter 保证是 dict（空 YAML / 损坏 → {}），
                # 故只需检查 turn_count 字段类型 + ≥0 取值（防手工误改产出 -5 等）
                tc = fm.get("turn_count")
                if isinstance(tc, int) and tc >= 0:
                    prev_turn_count = tc
            except MemoryNotFound:
                # 首压路径：summary.md 还不存在 → prev_turn_count 维持 0
                pass

            # ─── step 4: delta 判定（自上次压缩起新增 ≥ N；force 降到 ≥ 1） ───
            # 与旧版同：避免「过阈后每轮压缩 = 成本 bug」（消息每轮 +2，过 6 后会每轮跑 LLM）
            min_delta = 1 if force else every_n_messages
            if msg_count - prev_turn_count < min_delta:
                return

            # ─── step 5: 拼 convo：§21 递归累积（旧 summary + 仅自水位线起的新增消息） ───
            # messages[prev_turn_count:] 取自上次压缩水位线起的新增 messages
            # 旧 summary 已被 LLM 浓缩 → 不再二次浓缩老消息，token 输入恒有界
            new_msgs = messages[prev_turn_count:]
            parts: list[str] = []
            if prev_summary:
                parts.append("【已有会话摘要】\n" + prev_summary)
            # 守卫已保证 msg_count - prev_turn_count >= min_delta >= 1 → new_msgs 必非空；
            # 仍以 if 守一层，与 prev_summary 段对称且对未来阈值改动稳健
            if new_msgs:
                parts.append(
                    "【新增对话】\n"
                    + "\n".join(
                        # [role] 前缀 + content 截 200 字（与旧版一致）；
                        # 截断防单条消息撑爆 LLM context；m.content 可能 None → 默认 ""
                        f"[{m.role}] {(m.content or '')[:200]}" for m in new_msgs
                    )
                )
            # 两段间用 \n\n 分隔（同旧版 service.py:152）
            convo = "\n\n".join(parts)

            # ─── step 6: LLM 调用 ───
            summary = await self._llm.complete(system=_SESSION_COMPACT_SYSTEM, user=convo)
            # strip 防 LLM 返带前后空白；None 防鸭子 LLM 实现失误返 None
            summary = (summary or "").strip()
            if not summary:
                # LLM 返空 → 不写文件（旧 summary.md 维持，下轮 delta 守卫重试）
                return

            # ─── step 7: focus_entity_ids 聚合（复用 service.py 既有 helper，S4/S5 共用） ───
            # messages[-12:] 取末 12 条（最近 ~6 轮）做 focus 主题判定；
            # _extract_focus_entity_ids 内部已防御 missing/bad metadata，不抛
            focus = _extract_focus_entity_ids(messages[-12:])

            # ─── step 8: 写新 summary.md（frontmatter + body，复用 S2 helper） ───
            fm_new = {
                "turn_count": msg_count,                  # 新水位线
                "focus_entity_ids": focus,                # 截 _FOCUS_MAX=10
                "updated_at": _now_iso_z(),               # ISO 8601 Z（S4 helper 复用）
            }
            # _render_frontmatter(meta, body) 真签名：返 "---\n{YAML}---\n{body}"
            # 调用方约定 body 自带末尾换行（S2 _render_frontmatter docstring 明示）
            content = _render_frontmatter(fm_new, summary + "\n")
            # fs.write 原子写（S1 os.replace POSIX rename）；并发安全
            await fs.write(uri, content)

        except Exception:
            # 中层失败语义（§6.5）：整体 try/except → debug 留痕 + return None，绝不抛
            # 记忆是辅助（§4.3），主答绝不受影响
            _log.debug(
                "SessionCompactor.compact failed for session %s, silently ignored",
                session_id, exc_info=True,
            )
            return
