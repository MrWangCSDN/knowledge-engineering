"""跨工程归档 session 汇总 API（用户维度，不限定 project_id）。

设计文档：[[会话归档-设计]] §5.1, §5.5
"""
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.service.auth_dependencies import get_current_user
from src.service.auth_models import User
from src.service.db import get_db
from src.service.db_models_homepage import Project as ProjectModel, QASession

# prefix=/api/user：用户维度（不绑定 project），与 qa_router 的 /projects/.. 平级
router = APIRouter(prefix="/api/user", tags=["user-archived"])


class _ArchivedSessionDTO(BaseModel):
    """归档列表行：仅返回 metadata 字段。"""
    id: str
    title: Optional[str] = None
    archived_at: str
    created_at: str
    updated_at: str
    message_count: int


class _ProjectGroupDTO(BaseModel):
    """归档列表按 project 分组。"""
    project_id: str
    project_name: str
    sessions: list[_ArchivedSessionDTO]


class ArchivedListResponse(BaseModel):
    """GET /api/user/archived-sessions 响应。"""
    by_project: list[_ProjectGroupDTO]


@router.get("/archived-sessions")
async def list_archived_sessions(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ArchivedListResponse:
    """列出当前用户所有工程的归档 session，按工程分组。
    设计：[[会话归档-设计]] §5.5。"""
    # 1. 查所有归档（按 user_id 过滤，复合索引 user_id + archived_at 覆盖）
    stmt = (
        select(QASession)
        .where(QASession.user_id == user.id, QASession.archived_at.is_not(None))
        .order_by(QASession.archived_at.desc())
    )
    rows = (await db.execute(stmt)).scalars().all()

    # 2. 收集涉及的 project_id 集合，一次性查 project_name
    project_ids = sorted({r.project_id for r in rows})
    if project_ids:
        proj_stmt = select(ProjectModel).where(ProjectModel.id.in_(project_ids))
        projects = {p.id: p for p in (await db.execute(proj_stmt)).scalars().all()}
    else:
        projects = {}

    # 3. 按 project_id 分桶
    by_pid: dict[str, list[_ArchivedSessionDTO]] = {pid: [] for pid in project_ids}
    for r in rows:
        by_pid[r.project_id].append(_ArchivedSessionDTO(
            id=r.id,
            title=r.title,
            archived_at=r.archived_at.isoformat(),
            created_at=r.created_at.isoformat(),
            updated_at=r.updated_at.isoformat(),
            message_count=r.message_count,
        ))

    # 4. 按 project_name 排序输出
    groups = sorted(
        [
            _ProjectGroupDTO(
                project_id=pid,
                project_name=projects.get(pid).name if pid in projects else pid,
                sessions=sess_list,
            )
            for pid, sess_list in by_pid.items()
        ],
        key=lambda g: g.project_name or "",
    )
    return ArchivedListResponse(by_project=groups)
