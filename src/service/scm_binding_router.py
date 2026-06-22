# src/service/scm_binding_router.py
"""工程 SCM 绑定 + 索引触发。设计 §8/§9。
现已挂 require_project_role（bind/reindex=maintainer, index-status=reporter）；TODO(P4)：叠加 SCM-role 门禁。"""
from __future__ import annotations

import logging  # 标准库日志模块
from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, desc

# 模块级 logger：日志名遵循 ke.scm.bind 命名空间，方便运维按前缀过滤
_log = logging.getLogger("ke.scm.bind")

from src.service.db_models_homepage import Project, IndexJob, ScmConnection  # ScmConnection 供 bind 门查连接
from src.service.indexing.queue import enqueue_index_job
from src.service.permission_deps import require_project_role  # KE RBAC 权限工厂
from src.service.scm.scm_authz import flag_on   # 读环境变量 kill-switch（与 qa_router 共用范式）
from src.service.scm.base import ScmRole          # 三档权限枚举（CAN_BIND / CAN_QUERY / NOT_VISIBLE）


class BindRequest(BaseModel):
    """工程绑定请求体：把某个 SCM 连接下的某仓某分支绑定到工程。"""
    connection_id: str
    repo_external_id: int
    repo_full_name: str
    ref: str
    ref_type: str = "branch"
    subpath: Optional[str] = None


def create_scm_binding_routes(
    *,
    get_current_user: Callable,
    get_db: Callable,
    require_role: Callable = require_project_role,    # 可注入；默认用真实 RBAC；单测可传 no-op
    authorize_scm: Optional[Callable] = None,         # SCM 门；None=不启用（api.py 装配前默认关）
) -> APIRouter:
    router = APIRouter(tags=["scm-binding"])

    @router.post("/projects/{project_id}/bind",
                 dependencies=[Depends(require_role("maintainer"))])  # maintainer 以上才能绑定
    async def bind(project_id: str, body: BindRequest,
                   user=Depends(get_current_user), db=Depends(get_db)) -> dict:
        p = await db.get(Project, project_id)
        if p is None:
            raise HTTPException(status_code=404, detail="工程不存在")
        # SCM-role 门禁（KE_SCM_BIND_AUTHZ=1 时尝试激活）。
        # flag 默认关——现有测试零回归；PAT 连接走纯 KE-RBAC，跳过 SCM 门。
        # spec I6：flag 已开但 authorize_scm 未接线时，打 WARNING 而非静默透过。
        if flag_on("KE_SCM_BIND_AUTHZ"):
            if authorize_scm is None:
                # 装配漏了：kill-switch 已翻但工厂没传 authorize_scm → 门实际未生效，
                # 用 WARNING 提示运维而不是直接 500 / 阻断请求（安全 tripwire，不影响业务）
                _log.warning(
                    "KE_SCM_BIND_AUTHZ 已开但 authorize_scm 未接线，bind SCM 门未生效（装配漏了？）"
                )
            else:
                # body.connection_id 是调用者可自由填的，必须先确认连接存在（否则 404）。
                # 这与 QA 门不同：QA 读的是已校验的 project.scm_connection_id，永不 404。
                conn = await db.get(ScmConnection, body.connection_id)
                if conn is None:
                    raise HTTPException(status_code=404, detail="连接不存在")
                if conn.auth_type != "pat":      # PAT → 跳过 SCM 门（纯 KE-RBAC 已够）
                    role = await authorize_scm(
                        db, user=user, conn=conn,
                        repo_full_name=body.repo_full_name,
                        repo_external_id=body.repo_external_id,
                        need_bind=True,
                    )
                    if role != ScmRole.CAN_BIND:
                        raise HTTPException(status_code=403, detail="无该仓 maintainer/admin 权限，不能绑定")
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

    @router.post("/projects/{project_id}/reindex",
                 dependencies=[Depends(require_role("maintainer"))])  # maintainer 以上才能触发重建索引
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

    @router.get("/projects/{project_id}/index-status",
                dependencies=[Depends(require_role("reporter"))])  # reporter 以上可查看索引状态
    async def index_status(project_id: str, user=Depends(get_current_user), db=Depends(get_db)) -> dict:
        job = (await db.execute(
            select(IndexJob).where(IndexJob.project_id == project_id)
            .order_by(desc(IndexJob.created_at)).limit(1)
        )).scalars().first()
        if job is None:
            raise HTTPException(status_code=404, detail="无索引作业")
        return {"job_id": job.id, "status": job.status, "progress": job.progress, "error": job.error}

    return router
