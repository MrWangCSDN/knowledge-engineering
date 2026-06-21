# src/service/scm_binding_router.py
"""工程 SCM 绑定 + 索引触发。设计 §8/§9。
'谁能绑'用现有 KE RBAC；TODO(P4)：叠加 SCM-role 门禁（owner/maintainer 才能绑）。"""
from __future__ import annotations

from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, desc

from src.service.db_models_homepage import Project, IndexJob
from src.service.indexing.queue import enqueue_index_job


class BindRequest(BaseModel):
    """工程绑定请求体：把某个 SCM 连接下的某仓某分支绑定到工程。"""
    connection_id: str
    repo_external_id: int
    repo_full_name: str
    ref: str
    ref_type: str = "branch"
    subpath: Optional[str] = None


def create_scm_binding_routes(*, get_current_user: Callable, get_db: Callable) -> APIRouter:
    router = APIRouter(tags=["scm-binding"])

    @router.post("/projects/{project_id}/bind")
    async def bind(project_id: str, body: BindRequest,
                   user=Depends(get_current_user), db=Depends(get_db)) -> dict:
        p = await db.get(Project, project_id)
        if p is None:
            raise HTTPException(status_code=404, detail="工程不存在")
        # TODO(P4)：SCM-role 门禁——校验 user 在该仓是 owner/maintainer 才放行。
        p.scm_connection_id = body.connection_id
        p.repo_external_id = body.repo_external_id
        p.repo_full_name = body.repo_full_name
        p.ref = body.ref
        p.ref_type = body.ref_type
        p.subpath = body.subpath
        p.status = "indexing"
        job = await enqueue_index_job(db, project_id=project_id, type_="full_index", trigger="manual")
        await db.commit()
        return {"job_id": job.id}

    @router.post("/projects/{project_id}/reindex")
    async def reindex(project_id: str, user=Depends(get_current_user), db=Depends(get_db)) -> dict:
        p = await db.get(Project, project_id)
        if p is None:
            raise HTTPException(status_code=404, detail="工程不存在")
        # 未绑定 SCM 的工程无法重新索引，排队只会让 worker 失败——直接拒绝。
        if not p.scm_connection_id:
            raise HTTPException(status_code=422, detail="工程尚未绑定 SCM 仓库，请先调用 /bind")
        job = await enqueue_index_job(db, project_id=project_id, type_="reindex", trigger="manual")
        await db.commit()
        return {"job_id": job.id}

    @router.get("/projects/{project_id}/index-status")
    async def index_status(project_id: str, user=Depends(get_current_user), db=Depends(get_db)) -> dict:
        job = (await db.execute(
            select(IndexJob).where(IndexJob.project_id == project_id)
            .order_by(desc(IndexJob.created_at)).limit(1)
        )).scalars().first()
        if job is None:
            raise HTTPException(status_code=404, detail="无索引作业")
        return {"job_id": job.id, "status": job.status, "progress": job.progress, "error": job.error}

    return router
