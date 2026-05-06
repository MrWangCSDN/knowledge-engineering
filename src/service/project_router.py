"""项目（工程）管理路由。

设计文档：[[首页设计]] §6.2

路由：
  GET    /api/projects              列出当前用户可访问的工程
  GET    /api/projects/{id}         工程详情
  POST   /api/projects               创建（admin only；v1 主要给 CLI 用，前端 v2 才暴露）

未来（v2）：
  PATCH  /api/projects/{id}/status   admin 切状态
  DELETE /api/projects/{id}          admin 删工程
"""
from __future__ import annotations

from datetime import timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.service.auth_dependencies import get_current_user
from src.service.auth_models import User
from src.service.db import get_db
from src.service.db_models_homepage import Project as ProjectModel
from src.service.project_models import (
    IndexingProgress,
    Project,
    ProjectCreateRequest,
    ProjectListResponse,
    ProjectStats,
)


router = APIRouter(prefix="/api/projects", tags=["projects"])


# ─── 工具：ORM → Pydantic 转换 ──────────────────────────────────────────────

def _to_pydantic(p: ProjectModel) -> Project:
    """ORM Project → Pydantic Project。

    indexing_progress JSON 字段同时承载 stats（methods_count 等）
    和 indexing 进度（phase/percent/eta_seconds）—— 字段平铺，按 status 决定取哪些。
    """
    raw = p.indexing_progress or {}

    stats = ProjectStats(
        methods_count=raw.get("methods_count", 0),
        classes_count=raw.get("classes_count", 0),
        interpretation_progress=raw.get("interpretation_progress", 0),
    )

    indexing = None
    if p.status == "indexing" and "phase" in raw:
        indexing = IndexingProgress(
            phase=raw["phase"],
            percent=raw.get("percent", 0),
            eta_seconds=raw.get("eta_seconds", 0),
        )

    pipeline_at = None
    if p.pipeline_at:
        # SQLAlchemy 返回的是 naive datetime（DB 没存时区）；转 UTC ISO 加 Z 后缀
        dt = p.pipeline_at.replace(tzinfo=timezone.utc) if p.pipeline_at.tzinfo is None else p.pipeline_at
        pipeline_at = dt.isoformat().replace("+00:00", "Z")

    return Project(
        id=p.id,
        name=p.name,
        status=p.status,  # type: ignore[arg-type]
        stats=stats,
        pipeline_at=pipeline_at,
        indexing_progress=indexing,
    )


# ─── 路由 ───────────────────────────────────────────────────────────────────

@router.get("", response_model=ProjectListResponse)
async def list_projects(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> ProjectListResponse:
    """列出当前用户可访问的工程。

    v1：所有登录用户都能看到所有工程。
    v2：根据 user_project_access 表过滤。
    """
    stmt = select(ProjectModel).order_by(ProjectModel.created_at.desc())
    result = await db.execute(stmt)
    projects = result.scalars().all()
    return ProjectListResponse(projects=[_to_pydantic(p) for p in projects])


@router.get("/{project_id}", response_model=Project)
async def get_project(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> Project:
    p = await db.get(ProjectModel, project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="工程不存在")
    return _to_pydantic(p)


@router.post("", response_model=Project, status_code=status.HTTP_201_CREATED)
async def create_project(
    body: ProjectCreateRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Project:
    """admin only。v1 主要给 CLI 工具用；前端 v2 暴露表单。"""
    if not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="仅管理员可创建工程",
        )

    existing = await db.get(ProjectModel, body.id)
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"工程 ID 已存在: {body.id}",
        )

    p = ProjectModel(
        id=body.id,
        name=body.name,
        repo_url=body.repo_url,
        language=body.language,
        status="indexing",   # 默认 indexing；pipeline 跑完后改 ready
        created_by=user.username,
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return _to_pydantic(p)
