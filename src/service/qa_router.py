"""问答路由 - SSE 流式接口。

设计文档：[[首页设计]] §6.2 + §6.4

路由：
  POST /api/projects/{pid}/qa/explain    流式问答（SSE）

依赖（Task 22 per-request 模式）：
  app.state.weaviate_business_store  WeaviateBusinessInterpretStore singleton
  app.state.neo4j_backend            Neo4jGraphBackend singleton
  app.state.qa_synthesizer           QASynthesizer 实例（startup 注入，singleton）
  app.state.qa_router                SkillRouter 实例（startup 注入，singleton）

向后兼容（旧 singleton 模式，用于测试夹具）：
  如果 app.state.qa_retriever 已经挂上，explain endpoint 会直接用它
  而不走 per-request 构造，保证现有测试不破。
"""
from __future__ import annotations

import logging
import uuid
from datetime import timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.service.auth_dependencies import get_current_user
from src.service.auth_models import User
from src.service.db import get_db
from src.service.db_models_homepage import (
    Project as ProjectModel,
    QAFeedback,
    QAMessage,
    QASession,
)
from src.service.qa_engine.docx_exporter import build_docx, build_docx_from_template
from src.service.qa_engine.prompts import _TITLE_SUMMARY_SYSTEM
from src.service.qa_engine.sse_emitter import stream_qa_answer
from src.service.memory.service import (
    recall_memory_block,
    maybe_compact_session,
)

# v2.0 Task 5：引入权限 dependency 工厂
# require_project_role(min_role) 返回一个 FastAPI dependency（async 闭包）：
#   - 从 URL path 提取 project_id
#   - 查 user_project_access 表 + group 继承链，计算用户在该工程的 role
#   - Instance Admin（is_admin=True）直接视为 owner，不受影响
#   - 若 role < min_role（或无任何成员关系），抛出 HTTPException(403)
# 用法：在 @router.xxx(..., dependencies=[Depends(require_project_role("reporter"))]) 中声明
# FastAPI 的 dependencies 参数：不需要路由函数参数接收返回值，只需副作用（权限检查）时使用
from src.service.permission_deps import require_project_role

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/projects/{project_id}/qa", tags=["qa"])


# ─── per-request 构造 helpers（Task 22） ─────────────────────────────────────


def build_retriever_for_project(project_id: str, request: Request):
    """每个请求构造一个绑定 project_id 的 QARetriever。

    使用 app.state 里的 singleton 底层资源（weaviate_business_store + neo4j_backend），
    为每个请求分别构造 adapter，确保 Neo4jGraphAdapter 绑定正确的 project_id，
    实现多租户数据隔离。

    :param project_id: URL path 中的工程 ID，非空字符串
    :param request: FastAPI Request，用于访问 app.state singleton 资源
    :return: QARetriever 实例（已绑定 project_id）
    :raises RuntimeError: weaviate_business_store 或 neo4j_backend 未就绪时抛出
    """
    # 从 app.state 取 singleton 底层资源（startup 时已连接）
    biz_store = getattr(request.app.state, "weaviate_business_store", None)
    neo4j_backend = getattr(request.app.state, "neo4j_backend", None)

    if biz_store is None or neo4j_backend is None:
        raise RuntimeError(
            "底层存储资源未就绪（weaviate_business_store / neo4j_backend）"
        )

    # 延迟导入：避免 module-level 循环依赖
    from src.service.qa_engine.adapters import Neo4jGraphAdapter, WeaviateBusinessAdapter
    from src.service.qa_engine.retriever import QARetriever

    # 每个请求构造新的 adapter 实例：
    # WeaviateBusinessAdapter：包装 singleton store，本身无状态，轻量
    biz_adapter = WeaviateBusinessAdapter(biz_store)
    # Neo4jGraphAdapter：绑定 project_id，所有 Cypher 查询带 project_id 过滤
    graph_adapter = Neo4jGraphAdapter(neo4j_backend, project_id=project_id)

    # QARetriever：注入上面两个 adapter，本请求专用
    return QARetriever(business_store=biz_adapter, graph=graph_adapter)


def build_tools_for_project(project_id: str, request: Request):
    """每个请求构造一个绑定 project_id 的 ToolRegistry。

    与 build_retriever_for_project 同理：adapter 按 project_id 绑定，
    工具里的 handler 通过 input dict 的 project_id 字段传递隔离信息（ke_search 等）。

    :param project_id: URL path 中的工程 ID
    :param request: FastAPI Request
    :return: ToolRegistry 实例（已绑定 project_id 的 adapter）
    :raises RuntimeError: 底层资源未就绪时抛出
    """
    biz_store = getattr(request.app.state, "weaviate_business_store", None)
    neo4j_backend = getattr(request.app.state, "neo4j_backend", None)

    if biz_store is None or neo4j_backend is None:
        raise RuntimeError(
            "底层存储资源未就绪（weaviate_business_store / neo4j_backend）"
        )

    from src.service.qa_engine.adapters import Neo4jGraphAdapter, WeaviateBusinessAdapter
    from src.service.qa_engine.tools import build_default_registry

    biz_adapter = WeaviateBusinessAdapter(biz_store)
    graph_adapter = Neo4jGraphAdapter(neo4j_backend, project_id=project_id)

    return build_default_registry(graph=graph_adapter, business_store=biz_adapter)


# ─── 请求体 ─────────────────────────────────────────────────────────────────

class ExplainRequest(BaseModel):
    """POST /qa/explain body。"""
    question: str = Field(..., min_length=1, max_length=2000)
    session_id: Optional[str] = None
    """已有会话的 id（追问场景）；为空则新建会话。"""

    history: Optional[list[dict]] = None
    """上下文历史（最近 N 条消息，前端按需传）。"""


# ─── 主路由：SSE 流式问答 ──────────────────────────────────────────────────

@router.post(
    "/explain",
    # dependencies 列表：FastAPI 在执行路由函数前，按顺序调用这里的每个 dependency
    # Depends(require_project_role("reporter"))：
    #   - require_project_role("reporter") 是工厂函数调用，返回一个 async 闭包（checker）
    #   - Depends(checker) 告诉 FastAPI：请在处理请求前调用 checker 做权限验证
    #   - 若权限不足，checker 抛出 HTTPException(403)，路由函数根本不会被执行
    # "reporter" 是最低权限：工程的任何正式成员（含 reporter/maintainer/owner）均可问答
    dependencies=[Depends(require_project_role("reporter"))],
)
async def explain(
    project_id: str,
    body: ExplainRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """流式问答。

    错误：
      404 工程不存在
      409 工程正在索引（暂不可问答）
      422 question 为空（Pydantic 校验）
    """
    # 1. 工程存在性 + 状态校验
    p = await db.get(ProjectModel, project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="工程不存在")
    if p.status == "indexing":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="工程正在索引，暂时无法问答；完成后会自动通知"
        )

    # 2. 复用会话或新建
    session_id = body.session_id
    is_new_session = session_id is None

    # 检查已有会话是否已归档（仅当 session_id 被提供时）
    if not is_new_session:
        existing_session = await db.get(QASession, session_id)
        if existing_session and existing_session.archived_at is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="该会话已归档，请先恢复后继续提问"
            )

    if is_new_session:
        session_id = "sess_" + uuid.uuid4().hex[:12]
        sess = QASession(
            id=session_id,
            project_id=project_id,
            user_id=user.id,
            title=body.question[:30],
            message_count=0,
        )
        db.add(sess)
        await db.commit()

    # 3. 获取 retriever / synthesizer / router
    # Task 22 per-request 构造：
    #   优先检查 app.state.qa_retriever（测试夹具注入路径，向后兼容）
    #   若不存在则走 per-request 构造（生产路径，按 project_id 隔离）
    _app = request.app
    synthesizer = getattr(_app.state, "qa_synthesizer", None)
    # router 是可选的（v1.1 引入）；缺失时 SSE 不带 skill 字段，向后兼容
    skill_router = getattr(_app.state, "qa_router", None)

    # 向后兼容路径：测试 fixture 在 app.state.qa_retriever 上挂了 mock
    _legacy_retriever = getattr(_app.state, "qa_retriever", None)
    if _legacy_retriever is not None:
        # 旧路径（singleton / mock）：直接使用，不走 per-request 构造
        retriever = _legacy_retriever
    else:
        # Task 22 新路径（per-request）：按 project_id 构造绑定的 QARetriever
        _biz_store = getattr(_app.state, "weaviate_business_store", None)
        _neo4j = getattr(_app.state, "neo4j_backend", None)
        if _biz_store is None or _neo4j is None:
            raise HTTPException(
                status_code=503,
                detail="QA 引擎未就绪（底层存储资源不可用）",
            )
        try:
            retriever = build_retriever_for_project(project_id, request)
        except Exception as e:
            raise HTTPException(
                status_code=503,
                detail=f"QA 引擎未就绪（per-request retriever 构造失败）: {e}",
            )

    if synthesizer is None:
        raise HTTPException(
            status_code=503,
            detail="QA 引擎未就绪（app.state.qa_synthesizer 缺失）",
        )

    # 4. 持久化回调（在 SSE 流末尾被调用）
    # captures: db, session_id, project_id, user
    async def persist_messages(
        question: str, sections: list[dict], metadata: dict
    ) -> None:
        """流完成后写 user 消息 + assistant 消息到 qa_messages 表。"""
        # 注意：emitter 的 done event 还没发出去，这里 db 操作必须快（< 500ms）
        async with db.begin_nested() if db.in_transaction() else _noop_ctx():
            user_msg = QAMessage(
                id="msg_" + uuid.uuid4().hex[:12],
                session_id=session_id,
                role="user",
                content=question,
            )
            assistant_msg = QAMessage(
                id="msg_" + uuid.uuid4().hex[:12],
                session_id=session_id,
                role="assistant",
                content=None,
                sections=sections,
                msg_metadata=metadata,
            )
            db.add_all([user_msg, assistant_msg])

            # 更新会话 message_count
            sess = await db.get(QASession, session_id)
            if sess is not None:
                sess.message_count = (sess.message_count or 0) + 2
        await db.commit()

    # 5. 记忆召回（文件式记忆 S3：Weaviate 向量召回 on L0）：embed body.question
    # 在用户租户内做 near_vector top-k，命中目录 L0 时同目录 fs.read .overview.md
    # 展开；失败 S3 自包返 ""（with_memory_block 零开销不注入路径）。
    # 防御性 try 保留（深度防护；S3 自身已 try → 返 ""，此层是冗余兜底）。
    try:
        # fs 由 KE 部署期单例提供；按 KE 既有 memory.vfs 入口构造
        from src.service.memory.vfs import MemoryFS
        memory_block = await recall_memory_block(
            MemoryFS(),                         # 默认 root 由 KE_MEM_ROOT 环境变量或仓库根派生
            body.question,                       # query 来自 body.question（当前用户问题）
            user_id=user.id,
            top_k=5,
        )
    except Exception:
        memory_block = ""

    # 6. 会话上下文压力（spec §18）：按 token 预算裁 body.history 只留最近若干轮，
    #    更早轮由 system 记忆块 working_summary+focus 顶替。失败退回原行为，不抛。
    try:
        from src.service.memory.context_budget import (
            history_token_budget, trim_history_to_budget,
            estimate_tokens, model_context_window,
        )
        _budget = history_token_budget()
        eff_history, _hist_used = trim_history_to_budget(body.history, _budget)
        _raw_n = len(body.history) if isinstance(body.history, list) else 0
        history_trimmed = _raw_n > len(eff_history)
        _window = model_context_window()
        _used = estimate_tokens(memory_block) + estimate_tokens(body.question) + _hist_used
        context_usage = {
            "used_tokens": _used,
            "window_tokens": _window,
            "pct": round(min(_used / _window, 1.0) * 100, 1) if _window else 0.0,
            "history_trimmed": history_trimmed,
        }
    except Exception:
        eff_history = body.history
        history_trimmed = False
        context_usage = None

    # ⚠️ db session 在整个 StreamingResponse 迭代期间保持打开：Starlette 在依赖
    #    yield 退出后才迭代响应体，故所有流后回调（_make_title_generator /
    #    _make_memory_writer）都依赖此点。若将来把 stream_qa_answer 挪到后台任务/
    #    worker，必须为这些回调重新划定 db session 作用域，否则 session 已关闭。
    # 7. 返回 SSE 流
    return StreamingResponse(
        stream_qa_answer(
            question=body.question,
            project_id=project_id,
            session_id=session_id,
            retriever=retriever,
            synthesizer=synthesizer,
            router=skill_router,  # 可能是 None，sse_emitter 内部处理
            history=eff_history,
            on_complete=persist_messages,
            on_title=_make_title_generator(
                db=db,
                session_id=session_id,
                question=body.question,
                llm=synthesizer.llm,
                is_new_session=is_new_session,
            ),
            memory_block=memory_block,
            context_usage=context_usage,
            on_memory=_make_memory_writer(
                db=db,
                llm=synthesizer.llm,
                user_id=user.id,
                session_id=session_id,
                question=body.question,
                project_id=project_id,
                force_compact=history_trimmed,
            ),
        ),
        media_type="text/event-stream",
        headers={
            # 阻止代理（nginx）缓冲，否则 SSE 体验崩
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ─── 工具：no-op async context manager ──────────────────────────────────────

class _noop_ctx:
    """当 session 不在 transaction 中时的占位，避免 begin_nested 抛错。"""
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False


# ─── 工具：DateTime ISO 格式化 ─────────────────────────────────────────────

def _iso(dt) -> str:
    """SQLAlchemy 返的 naive datetime → 带 Z 后缀的 ISO 8601。"""
    if dt is None:
        return ""
    fixed = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    return fixed.isoformat().replace("+00:00", "Z")


# ─── 会话列表 / 详情 / 删除 ───────────────────────────────────────────────

@router.get(
    "/sessions",
    # 列出会话：同样需要工程成员身份（reporter 及以上）
    # 非成员不应知道工程有哪些对话历史
    dependencies=[Depends(require_project_role("reporter"))],
)
async def list_sessions(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """列出当前用户在该工程下的会话（按 updated_at 倒序）。"""
    stmt = (
        select(QASession)
        .where(
            QASession.project_id == project_id,
            QASession.user_id == user.id,
            QASession.archived_at.is_(None),  # 默认只返回活动 session（归档的从此接口看不见）
        )
        .order_by(QASession.updated_at.desc())
    )
    result = await db.execute(stmt)
    sessions = result.scalars().all()
    return {
        "sessions": [
            {
                "id": s.id,
                "project_id": s.project_id,
                "title": s.title,
                "created_at": _iso(s.created_at),
                "updated_at": _iso(s.updated_at),
                "message_count": s.message_count,
            }
            for s in sessions
        ]
    }


@router.get(
    "/sessions/{session_id}",
    # 获取会话详情：需要工程成员身份
    # 路由函数内部还会再校验 sess.user_id == user.id（只有会话所有者能查自己的对话）
    # require_project_role 是第一道门（是否是工程成员），内部校验是第二道门（是否是会话所有者）
    dependencies=[Depends(require_project_role("reporter"))],
)
async def get_session_detail(
    project_id: str,
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """会话详情 + 消息（按 created_at 顺序）。

    设计上其他人的会话也返回 404（不暴露存在性）。
    """
    sess = await db.get(QASession, session_id)
    if not sess or sess.project_id != project_id or sess.user_id != user.id:
        raise HTTPException(status_code=404, detail="会话不存在")

    # 关联读消息（lazy load 用 await db.refresh 或显式 select）
    #
    # ⚠️ created_at tie-breaker（2026-05-16 修）：
    # persist_messages 把 user + assistant 两条消息在同一个 db.commit() 里写入，
    # created_at 用 server_default=func.now()（MySQL DATETIME 秒级精度）→ 两条
    # 时间戳完全相同。id 是随机 UUID，不是自增，所以 ORDER BY created_at 遇到
    # 并列时 tie-break 随机 → assistant 有时排到 user 前面（前端表现为"答在问之上"）。
    # 修复：同一 created_at 时，强制 user(0) 排在 assistant(1) 前。
    msgs_stmt = (
        select(QAMessage)
        .where(QAMessage.session_id == session_id)
        .order_by(
            QAMessage.created_at,
            case((QAMessage.role == "user", 0), else_=1),
        )
    )
    msgs = (await db.execute(msgs_stmt)).scalars().all()

    return {
        "session": {
            "id": sess.id,
            "project_id": sess.project_id,
            "title": sess.title,
            "created_at": _iso(sess.created_at),
            "updated_at": _iso(sess.updated_at),
            "message_count": sess.message_count,
        },
        "messages": [
            {
                "id": m.id,
                "session_id": m.session_id,
                "role": m.role,
                "content": m.content,
                "sections": m.sections,
                "metadata": m.msg_metadata,
                "created_at": _iso(m.created_at),
            }
            for m in msgs
        ],
    }


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    # 删除会话：需要工程成员身份（reporter 及以上）
    # 路由函数内部同样校验 sess.user_id == user.id（只有会话所有者能删自己的对话）
    dependencies=[Depends(require_project_role("reporter"))],
)
async def delete_session(
    project_id: str,
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """删除会话 → 级联删 messages → 级联删 feedback。"""
    sess = await db.get(QASession, session_id)
    if not sess or sess.project_id != project_id or sess.user_id != user.id:
        raise HTTPException(status_code=404, detail="会话不存在")

    await db.delete(sess)
    await db.commit()


# ─── 异步标题总结 ───────────────────────────────────────────────────────────


def _make_title_generator(*, db, session_id, question, llm, is_new_session):
    """构造一个 on_title 回调（闭包）。

    仅当 is_new_session 且 session.title_custom==False 时调 LLM 总结，
    UPDATE+commit DB（先落库，DB 是 source of truth），返回新标题；
    否则 / 失败 返回 None（静默降级）。
    设计：[[会话标题-重命名与智能总结-设计]] §3.2
    """
    async def _gen():
        if not is_new_session:
            return None
        try:
            from src.service.db_models_homepage import QASession
            sess = await db.get(QASession, session_id)
            if sess is None or sess.title_custom:
                return None
            raw = await llm.complete(
                system=_TITLE_SUMMARY_SYSTEM,
                user=question,
            )
            title = (raw or "").strip().strip('"').strip("「」").strip()
            if not title:
                return None
            if len(title) > 30:
                title = title[:30]
            sess.title = title
            # title_custom 保持 False（系统生成）
            await db.commit()
            return title
        except Exception:
            return None  # 静默降级，绝不影响主流程

    return _gen


def _make_memory_writer(
    *,
    db,
    llm,
    user_id: int,
    session_id: str,
    question: str,
    project_id: str | None = None,
    force_compact: bool = False,
):
    """构造 on_memory 回调（闭包）。done 之后异步执行：

    1. S4 ReAct 抽取本轮可记忆事实 → 写文件 .md → S2.regenerate + S3.index_changed
    2. 会话消息达阈值 → 压缩会话工作状态（§22 暂留 DB tier，S5 后续迁文件）。

    全程异常静默（记忆是辅助，绝不影响主答）。
    设计：[[文件式记忆重构-设计]] §5.5。
    新签名：闭包返回 async def _writer(answer: str) — sse_emitter 传 assistant
    答案文本给闭包，ReAct 需要 user + assistant 两侧文本。
    """
    async def _writer(answer: str) -> None:
        # 1. S4：ReAct 抽取 + 文件写入 + S2/S3 链
        try:
            # 局部 import：避免顶部循环依赖（recall.py / extract.py / memgen.py
            # → service.py），与 service.recall_memory_block 同模式
            import os
            from src.service.memory.vfs import MemoryFS
            from src.service.memory.memgen import MemoryGen
            from src.service.memory.recall import (
                MemoryRecaller, MemoryL0Store, _DefaultEmbedder,
            )
            from src.service.memory.extract import MemoryExtractor

            # 构造 fs（默认 root 由 KE_MEM_ROOT 环境变量或仓库根派生）
            fs = MemoryFS()
            # MemoryGen + Weaviate 客户端（沿用 service.recall_memory_block 的
            # env 读取方式；S7 hardening 把这些提到 module-level singleton）
            memgen = MemoryGen(llm)
            url = os.getenv("WEAVIATE_URL", "http://127.0.0.1:8080")
            grpc_port = int(os.getenv("WEAVIATE_GRPC_PORT", "50051"))
            api_key = os.getenv("WEAVIATE_API_KEY") or None
            store = MemoryL0Store(
                url=url, grpc_port=grpc_port,
                collection_name="memory_l0", dimension=1024, api_key=api_key,
            )
            recaller = MemoryRecaller(
                embedder=_DefaultEmbedder(),
                weaviate_client=store._client,
            )
            extractor = MemoryExtractor(llm)
            # 拼本轮文本（user + assistant）喂 ReAct
            turn_text = f"用户：{question}\n助理：{answer}"
            await extractor.extract_and_persist(
                fs, memgen, recaller,
                user_id=user_id,
                turn_text=turn_text,
            )
        except Exception:
            _log.debug(
                "S4 ReAct extract failed for session %s, silently ignored",
                session_id, exc_info=True,
            )
        # 2. 会话压缩（§22 暂留 DB tier，S5 后续迁文件）
        try:
            await maybe_compact_session(
                db, llm, session_id=session_id, force=force_compact,
            )
        except Exception:
            pass

    return _writer


# ─── 重命名 ─────────────────────────────────────────────────────────────────


class RenameSessionBody(BaseModel):
    """PATCH /sessions/{sid} body：仅含新标题。"""
    title: str


@router.patch(
    "/sessions/{session_id}",
    # 重命名：需要工程成员身份（reporter+）；内部再校验 session owner
    dependencies=[Depends(require_project_role("reporter"))],
)
async def rename_session(
    project_id: str,
    session_id: str,
    body: RenameSessionBody,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """重命名会话。置位 title_custom=True，异步总结将永不覆盖。

    设计：[[会话标题-重命名与智能总结-设计]] §3.1
    """
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="标题不能为空")
    if len(title) > 100:
        raise HTTPException(status_code=400, detail="标题不能超过 100 字")

    sess = await db.get(QASession, session_id)
    if not sess or sess.project_id != project_id or sess.user_id != user.id:
        raise HTTPException(status_code=404, detail="会话不存在")
    if sess.archived_at is not None:
        raise HTTPException(status_code=409, detail="已归档会话不可重命名")

    sess.title = title
    sess.title_custom = True
    await db.commit()

    return {"id": sess.id, "title": sess.title, "title_custom": sess.title_custom}


# ─── 归档 / 恢复 ─────────────────────────────────────────────────────────────


class ArchiveResponse(BaseModel):
    """archive / unarchive 响应：仅返回 id + archived_at（恢复时为 null）。"""
    id: str
    archived_at: Optional[str] = None


@router.post(
    "/sessions/{session_id}/archive",
    # 设计 §5.1：archive 需要 session owner + project reporter+
    dependencies=[Depends(require_project_role("reporter"))],
)
async def archive_session(
    project_id: str,
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ArchiveResponse:
    """把 session 设为归档状态。幂等：已归档再调不更新时间戳。
    设计：[[会话归档-设计]] §5.1, §5.4。"""
    # owner 检查与 delete_session 同模式：保证只能动自己的 session
    sess = await db.get(QASession, session_id)
    if not sess or sess.project_id != project_id or sess.user_id != user.id:
        raise HTTPException(status_code=404, detail="会话不存在")

    # 幂等性（§5.4）：已归档 → 不更新时间戳
    if sess.archived_at is None:
        from datetime import datetime as _dt
        sess.archived_at = _dt.now(timezone.utc).replace(tzinfo=None)
        await db.commit()
        await db.refresh(sess)

    return ArchiveResponse(
        id=sess.id,
        archived_at=_iso(sess.archived_at) if sess.archived_at else None,
    )


@router.post(
    "/sessions/{session_id}/unarchive",
    # 设计 §5.1：unarchive 需要 session owner + project reporter+
    dependencies=[Depends(require_project_role("reporter"))],
)
async def unarchive_session(
    project_id: str,
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ArchiveResponse:
    """把归档 session 恢复为活动状态。幂等：本来就是活动的直接返回 null。
    设计：[[会话归档-设计]] §5.1, §5.4。"""
    sess = await db.get(QASession, session_id)
    if not sess or sess.project_id != project_id or sess.user_id != user.id:
        raise HTTPException(status_code=404, detail="会话不存在")

    # 幂等：活动 session 不需要变更
    if sess.archived_at is not None:
        sess.archived_at = None
        await db.commit()

    return ArchiveResponse(id=sess.id, archived_at=None)


# ─── 反馈 ───────────────────────────────────────────────────────────────────

class FeedbackRequest(BaseModel):
    """POST /sessions/{sid}/messages/{mid}/feedback body。"""
    vote: Literal["up", "down"]
    comment: Optional[str] = Field(None, max_length=2000)


@router.post(
    "/sessions/{session_id}/messages/{message_id}/feedback",
    status_code=status.HTTP_204_NO_CONTENT,
    # 提交反馈：需要工程成员身份（reporter 及以上）
    # 只有能访问工程问答的人，才有资格对 assistant 消息点赞/点踩
    dependencies=[Depends(require_project_role("reporter"))],
)
async def post_feedback(
    project_id: str,
    session_id: str,
    message_id: str,
    body: FeedbackRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """对一条 assistant 消息打反馈（覆盖式 upsert）。

    第二次对同一 message 的 feedback 直接覆盖第一次，不会留两条。
    """
    # 校验 message 存在 + 属于用户的 session
    msg = await db.get(QAMessage, message_id)
    if msg is None or msg.session_id != session_id:
        raise HTTPException(status_code=404, detail="消息不存在")

    sess = await db.get(QASession, session_id)
    if not sess or sess.project_id != project_id or sess.user_id != user.id:
        raise HTTPException(status_code=404, detail="会话不存在")

    # upsert：如果已有就更新，否则创建
    existing = await db.get(QAFeedback, message_id)
    if existing:
        existing.vote = body.vote
        existing.comment = body.comment
        existing.user_id = user.id
    else:
        db.add(QAFeedback(
            message_id=message_id,
            vote=body.vote,
            comment=body.comment,
            user_id=user.id,
        ))
    await db.commit()


# ─── v1.5 Word 导出 ────────────────────────────────────────────────────────


# docx MIME type — Word 文档专用 magic 字符串
# 客户端浏览器看到这个 + Content-Disposition 后会自动触发"另存为"
_DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


@router.get(
    "/sessions/{session_id}/messages/{message_id}/export",
    # 导出为 Word 文档：需要工程成员身份（reporter 及以上）
    # 防止外部人员通过猜测 URL 下载工程的问答记录
    dependencies=[Depends(require_project_role("reporter"))],
)
async def export_message(
    project_id: str,
    session_id: str,
    message_id: str,
    # `format=docx` 是 query 参数；不写 Literal 直接用 str 做手动校验
    # 方便后续加 pdf / html 等格式而不改路由签名
    format: str = Query("docx", description="导出格式；当前仅支持 docx"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    """导出 assistant 消息为 Word 文档。

    错误：
      400 format 不在支持列表里
      404 消息 / 会话 / 工程不存在
      403 用户不是会话所有者（其它人的对话不能下载）
    """
    # 1) 格式校验：早爆 400
    if format != "docx":
        raise HTTPException(
            status_code=400,
            detail=f"unsupported export format: {format!r}; only 'docx' is supported in v1.5",
        )

    # 2) 工程存在性
    project = await db.get(ProjectModel, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="工程不存在")

    # 3) 会话存在 + 属于当前用户
    sess = await db.get(QASession, session_id)
    if sess is None or sess.project_id != project_id:
        raise HTTPException(status_code=404, detail="会话不存在")
    if sess.user_id != user.id:
        # 不暴露会话存在的信息给非所有者；统一 404 即可
        raise HTTPException(status_code=404, detail="会话不存在")

    # 4) 消息存在 + 属于该会话
    msg = await db.get(QAMessage, message_id)
    if msg is None or msg.session_id != session_id:
        raise HTTPException(status_code=404, detail="消息不存在")

    # 5) 只导出 assistant 消息（user 消息没结构化内容）
    if msg.role != "assistant":
        raise HTTPException(
            status_code=400,
            detail="只能导出 assistant 消息，user 消息没有结构化答案",
        )

    # 6) 从同会话里找上一条 user 消息，作为问题原文
    # 实测多数情况就是 assistant 紧跟 user 之后
    user_msgs = (
        await db.execute(
            select(QAMessage)
            .where(QAMessage.session_id == session_id)
            .where(QAMessage.role == "user")
            .order_by(QAMessage.created_at)
        )
    ).scalars().all()
    # 取最近一条 user（v1.5 简化：不严格按时序匹配 assistant，下一版细化）
    # `if ... else ""` 是 Python 三元表达式
    question = user_msgs[-1].content if user_msgs else "(未知问题)"

    # 7) sections 在 DB 里以 JSON 存的（msg.sections 是 SQLAlchemy JSON 列）
    # 取出来给 build_docx；类型已经是 list[dict] 不用再 parse
    sections = msg.sections or []

    # 8) 构造 docx 字节
    # v1.9：环境变量 KE_DOCX_TEMPLATE_PATH 指向用户模板时，走模板路径
    # 不指定就走 v1.5 内置样式（兜底）
    import os
    template_path = os.environ.get("KE_DOCX_TEMPLATE_PATH", "").strip()
    if template_path and os.path.isfile(template_path):
        docx_bytes = build_docx_from_template(
            template_path=template_path,
            question=question,
            sections=sections,
            project_name=project.name or project.id,
        )
    else:
        docx_bytes = build_docx(
            question=question,
            sections=sections,
            project_name=project.name or project.id,
        )

    # 9) 生成下载文件名（不能太长 / 不能有特殊字符 / 用消息 id 兜底唯一）
    # short_id：避免文件名过长；message_id 形如 'msg_a1b2c3d4e5f6'
    short_id = message_id.replace("msg_", "")[:8]
    filename = f"qa-{project_id}-{short_id}.docx"

    # 10) Response 直接给 bytes + 关键头
    return Response(
        content=docx_bytes,
        media_type=_DOCX_MIME,
        headers={
            # `attachment` 让浏览器另存为；省略就在浏览器里内联展示（docx 不支持）
            # filename 兼容老 IE / 现代浏览器都用 RFC 5987 转义
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
