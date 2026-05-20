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

    frontmatter 损坏时 `_split_frontmatter` 降级返 ({}, 全文)，
    本函数取 body — body 是 frontmatter 闭合后的部分；闭合都没探到时
    `_split_frontmatter` 返 ({}, 原文)，本函数仍返裸文本作 summary（自愈优先，§6.5）。
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
        """post-turn 触发的会话压缩。算法 §6.4（T2 实现完整体）。"""
        # T2 step 1: 完整实现见后续提交；此处保留 stub 让 T1 测试可链接
        raise NotImplementedError("compact() implementation lands in S5 T2")
