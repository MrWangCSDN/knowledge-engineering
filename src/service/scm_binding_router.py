# src/service/scm_binding_router.py
"""工程 SCM 绑定 + 索引触发。设计 §8/§9。
现已挂 require_project_role（bind/reindex=maintainer, index-status=reporter）；TODO(P4)：叠加 SCM-role 门禁。"""
from __future__ import annotations

import logging  # 标准库日志模块
import os  # 标准库：读环境变量（KE_INDEX_LEASE_SECONDS）
from datetime import datetime, timezone, timedelta  # 时间处理：当前时间/时区/时间差
from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, desc, func, and_, or_  # func=聚合函数(count)；and_/or_=组合 WHERE 条件

# 模块级 logger：日志名遵循 ke.scm.bind 命名空间，方便运维按前缀过滤
_log = logging.getLogger("ke.scm.bind")

from src.service.db_models_homepage import Project, IndexJob, ScmConnection, UserProjectAccess  # ScmConnection 供 bind 门查连接；UserProjectAccess 供 create_project 写入 owner 权限
from src.service.indexing.queue import enqueue_index_job
from src.service.indexing.states import QUEUED, DONE, FAILED, PHASE_ORDER  # 作业状态常量 + 工作阶段顺序
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


class CreateProjectBindRequest(BaseModel):
    """自助连接：从某 SCM 连接的某仓创建工程并绑定。

    与 BindRequest 不同：此请求会新建 Project；BindRequest 是更新已有 Project。
    """
    # 工程业务可读 ID，如 'deposit-system'；后端不生成，由调用方传入
    project_id: str
    # 工程显示名，如 '存款系统'
    name: str
    # SCM 平台仓库的数字 ID（GitHub = repository.id，BigInteger 防溢出）
    repo_external_id: int
    # 仓库全名，格式 "owner/repo"，如 "myorg/deposit-service"
    repo_full_name: str
    # 要索引的 ref，如 'main'、'master'、'v2.0.0'
    ref: str
    # ref 类型：branch（默认）或 tag（语义不同，pipeline 区别对待）
    ref_type: str = "branch"
    # 子路径：仅索引仓库内某个子目录；None = 全仓
    subpath: Optional[str] = None


def _iso(dt):
    """datetime → ISO 字符串；None 透传。"""
    # 三元表达式：A if 条件 else B —— 等效于 if/else，写在一行更紧凑
    return dt.isoformat() if dt is not None else None


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

    @router.get("/projects/{project_id}/sync-health",
                dependencies=[Depends(require_role("reporter"))])  # reporter 以上可查看同步健康度
    async def sync_health(project_id: str, user=Depends(get_current_user), db=Depends(get_db)) -> dict:
        # 先取工程本体；不存在 → 404（与其他端点一致的契约）
        proj = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()
        if proj is None:
            raise HTTPException(status_code=404, detail="工程不存在")
        # 当前时间（带 UTC 时区），用于算 staleness 和判定卡死
        now = datetime.now(timezone.utc)
        # 租约秒数：默认 3600；os.getenv 第二参是默认值（环境变量缺省时返回）
        lease_seconds = int(os.getenv("KE_INDEX_LEASE_SECONDS", "3600"))
        # 最近一条作业：按 created_at 降序，并列时用 id 降序兜底（created_at 仅秒级精度）
        latest = (await db.execute(
            select(IndexJob).where(IndexJob.project_id == project_id)
            .order_by(desc(IndexJob.created_at), desc(IndexJob.id)).limit(1))).scalars().first()
        # None if ... else {...}：无作业返回 None，否则组装精简字段字典
        latest_job = None if latest is None else {
            "job_id": latest.id, "status": latest.status, "progress": latest.progress,
            "error": latest.error, "finished_at": _iso(latest.finished_at)}
        # 按 status 分组计数：SELECT status, COUNT(*) ... GROUP BY status
        rows = (await db.execute(
            select(IndexJob.status, func.count()).where(IndexJob.project_id == project_id)
            .group_by(IndexJob.status))).all()
        # 字典推导式：把 [(状态, 数量), ...] 转成 {状态: 数量}
        counts = {st: n for st, n in rows}
        # 三档聚合：running = 所有工作阶段(cloning/.../interpreting)计数之和
        job_counts = {
            "queued": counts.get(QUEUED, 0),
            "running": sum(counts.get(p, 0) for p in PHASE_ORDER),
            "failed": counts.get(FAILED, 0)}
        # 最新一条失败作业的 error 文本：按 finished_at 降序取首条
        last_error = (await db.execute(
            select(IndexJob.error).where(IndexJob.project_id == project_id, IndexJob.status == FAILED)
            .order_by(desc(IndexJob.finished_at)).limit(1))).scalars().first()
        # 卡死判定：在跑(非 queued/done/failed) 且 (租约已过 或 (无租约且 started_at 早于截止点))
        started_cutoff = now - timedelta(seconds=lease_seconds)
        running = IndexJob.status.notin_([QUEUED, DONE, FAILED])
        expired = or_(IndexJob.lease_expires < now,
                      and_(IndexJob.lease_expires.is_(None), IndexJob.started_at < started_cutoff))
        stuck_id = (await db.execute(
            select(IndexJob.id).where(IndexJob.project_id == project_id, running, expired).limit(1)
        )).scalars().first()
        # last_synced_at 可能是 naive datetime（SQLite 不存时区）→ 补成 UTC 再做差
        ls = proj.last_synced_at
        if ls is not None and ls.tzinfo is None:
            ls = ls.replace(tzinfo=timezone.utc)
        # 距上次同步的小时数（保留两位小数）；从未同步 → None
        staleness_hours = None if ls is None else round((now - ls).total_seconds() / 3600, 2)
        return {
            "project_id": project_id, "status": proj.status,
            "last_synced_at": _iso(proj.last_synced_at), "last_synced_commit": proj.last_synced_commit,
            "staleness_hours": staleness_hours, "is_stuck": stuck_id is not None,
            "latest_job": latest_job, "job_counts": job_counts, "last_error": last_error}

    @router.post("/scm/connections/{connection_id}/projects")
    async def create_project_from_connection(
        connection_id: str,                    # 路径参数：SCM 连接 ID
        body: CreateProjectBindRequest,        # Pydantic 自动从请求 JSON body 解析
        user=Depends(get_current_user),        # 从 JWT 解析出当前登录用户
        db=Depends(get_db),                    # 注入异步 DB session
    ) -> dict:
        """自助从 SCM 连接的某仓创建工程并入队首次全量索引。

        鉴权层级（按顺序，先核验后写入）：
          1. 连接归属（created_by == user.username）→ 404
          2. App 连接强制 SCM can_bind 门（PAT 以连接归属为门）→ 403
          3. project_id 冲突检查 → 409
          4. 建工程 + owner 授权 + 入队 full_index → 200

        Args:
            connection_id: 路径参数，SCM 连接的 ID（如 "conn-abc123"）
            body: 请求体，含工程 ID、仓库信息、ref 等
            user: 当前登录用户（从 JWT 解析）
            db: 异步 DB session（SQLAlchemy 2.0）
        Returns:
            {"project_id": str, "job_id": str}
        """
        # ── 步骤 1：连接归属校验 ────────────────────────────────────────────
        # db.get(Model, pk)：按主键查单条记录；连接不存在返回 None
        conn = await db.get(ScmConnection, connection_id)
        # getattr：防御性取属性，避免 user 对象意外缺少 username 属性
        caller_username = getattr(user, "username", None)
        # 三个判断合一：
        #   conn is None          → 连接不存在
        #   caller_username None  → user 异常（JWT 解析出问题）
        #   created_by 不匹配     → 连接属于他人
        # 统一 404：不泄露"连接属于他人"这一信息（防枚举他人连接 ID）
        if conn is None or caller_username is None or conn.created_by != caller_username:
            raise HTTPException(status_code=404, detail="连接不存在")

        # ── 步骤 2：SCM 授权门（App 连接必须 can_bind；PAT 以连接归属为门）──
        # PAT 连接：auth_type="pat"，连接归属已足够（步骤 1 通过即可），不调 authorize_scm
        if conn.auth_type != "pat":
            # 非 PAT（如 github_app）：必须有 authorize_scm；否则说明装配漏了 → 503
            if authorize_scm is None:
                # 工厂函数没传 authorize_scm，但连接是 App 类型，安全失败不放行
                raise HTTPException(status_code=503, detail="SCM 授权未装配")
            # 调用 authorize_scm 查询调用者对目标仓的实际 SCM 权限
            # need_bind=True：告知授权函数这是 bind 操作（GitHub App 可区分 read/write）
            role = await authorize_scm(
                db,
                user=user,
                conn=conn,
                repo_full_name=body.repo_full_name,
                repo_external_id=body.repo_external_id,
                need_bind=True,
            )
            # 只有 CAN_BIND（maintainer/admin）才能自助建工程
            if role != ScmRole.CAN_BIND:
                raise HTTPException(status_code=403, detail="无该仓 maintainer/admin 权限，不能连接")

        # ── 步骤 3：project_id 冲突检查 ─────────────────────────────────────
        # db.get(Project, pk)：按主键查 Project；不为 None 表示 ID 已占用
        if await db.get(Project, body.project_id) is not None:
            # 已存在同 ID 工程 → 409 Conflict，拒绝覆盖（幂等性保护）
            raise HTTPException(status_code=409, detail="工程 ID 已存在")

        # ── 步骤 4：建工程 + 创建者 owner + 入队首次全量索引 ───────────────────
        # 构造 Project ORM 对象，status="indexing" 表示已入队等待 worker
        p = Project(
            id=body.project_id,
            name=body.name,
            status="indexing",                          # 工程状态：等待首次索引
            scm_connection_id=connection_id,            # 绑定到本次用的 SCM 连接
            repo_external_id=body.repo_external_id,     # SCM 平台仓库数字 ID
            repo_full_name=body.repo_full_name,         # "owner/repo" 格式
            ref=body.ref,                               # 目标分支/tag
            ref_type=body.ref_type,                     # "branch" 或 "tag"
            subpath=body.subpath,                       # 子路径（可为 None）
            created_by=str(user.id),                    # 记录创建人 user.id（与 scm_router 约定一致）
        )
        # db.add(p)：把 ORM 对象纳入当前 session 追踪（相当于 INSERT 待执行）
        db.add(p)
        # 同时写入创建者的 owner 权限——让创建者能直接访问自己建的工程
        # UserProjectAccess.user_id 是 int（FK → users.id），user.id 本身就是 int
        db.add(UserProjectAccess(
            user_id=user.id,       # int；FK 约束指向 users.id
            project_id=body.project_id,
            role="owner",          # 创建者是 owner，拥有最高权限
        ))
        # await db.flush()：把 Project/UserProjectAccess 刷入事务（不 commit，但 DB 已可见）
        # 目的：enqueue_index_job 内部会 flush IndexJob，IndexJob.project_id 有外键约束
        # 必须先让 Project 在同一事务里可见，否则外键约束失败
        await db.flush()
        # 入队首次全量索引作业（type_="full_index"，trigger="manual"）
        # enqueue_index_job 内部会 session.add + session.flush，返回已 flush 的 IndexJob
        job = await enqueue_index_job(
            db,
            project_id=body.project_id,
            type_="full_index",   # 首次全量索引
            trigger="manual",     # 由用户手动触发（区别于 webhook 自动触发）
        )
        # await db.commit()：一次性提交所有写入：Project / UserProjectAccess / IndexJob
        # commit 后数据持久化，其他 session 才能看到
        await db.commit()
        # 返回工程 ID 和作业 ID，供前端跳转进度页（如 /projects/newp/index-status）
        return {"project_id": body.project_id, "job_id": job.id}

    return router
