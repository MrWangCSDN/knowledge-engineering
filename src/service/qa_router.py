"""问答路由 - SSE 流式接口。

设计文档：[[首页设计]] §6.2 + §6.4

路由：
  POST /api/projects/{pid}/qa/explain    流式问答（SSE）

依赖：
  app.state.qa_retriever     QARetriever 实例（startup 注入）
  app.state.qa_synthesizer   QASynthesizer 实例（startup 注入）

注：retriever / synthesizer 实例由 api.py startup 初始化时注入到 app.state。
   测试时 fixture 把 mock 实例直接挂上去（见 test_qa_router.py）。
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.service.auth_dependencies import get_current_user
from src.service.auth_models import User
from src.service.db import get_db
from src.service.db_models_homepage import (
    Project as ProjectModel,
    QAMessage,
    QASession,
)
from src.service.qa_engine.sse_emitter import stream_qa_answer


router = APIRouter(prefix="/api/projects/{project_id}/qa", tags=["qa"])


# ─── 请求体 ─────────────────────────────────────────────────────────────────

class ExplainRequest(BaseModel):
    """POST /qa/explain body。"""
    question: str = Field(..., min_length=1, max_length=2000)
    session_id: Optional[str] = None
    """已有会话的 id（追问场景）；为空则新建会话。"""

    history: Optional[list[dict]] = None
    """上下文历史（最近 N 条消息，前端按需传）。"""


# ─── 主路由：SSE 流式问答 ──────────────────────────────────────────────────

@router.post("/explain")
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

    # 3. 拿 app.state 注入的 retriever / synthesizer
    app = request.app
    retriever = getattr(app.state, "qa_retriever", None)
    synthesizer = getattr(app.state, "qa_synthesizer", None)
    if retriever is None or synthesizer is None:
        raise HTTPException(
            status_code=503,
            detail="QA 引擎未就绪（app.state.qa_retriever/qa_synthesizer 缺失）",
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

    # 5. 返回 SSE 流
    return StreamingResponse(
        stream_qa_answer(
            question=body.question,
            project_id=project_id,
            session_id=session_id,
            retriever=retriever,
            synthesizer=synthesizer,
            history=body.history,
            on_complete=persist_messages,
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
