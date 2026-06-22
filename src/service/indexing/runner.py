"""索引作业编排：认领一条 → 跑 indexer → 写进度/终态/重试。设计 §9。"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Awaitable, Callable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.service.db_models_homepage import IndexJob, Project
from src.service.indexing.queue import claim_next_job, reclaim_expired_jobs
from src.service.indexing.states import DONE, FAILED, QUEUED

MAX_ATTEMPTS = 3

ProgressCb = Callable[[str, int], Awaitable[None]]
IndexerFn = Callable[[IndexJob, ProgressCb], Awaitable[str]]


async def run_one_job(
    maker: async_sessionmaker[AsyncSession], *, worker_id: str, indexer: Optional[IndexerFn],
    lease_seconds: int = 3600,
) -> bool:
    """认领并处理一条作业。返回 True=处理了一条，False=队列空。

    认领前先 reclaim 过期租约（机会式 reaper）：过期 running → queued/failed，
    本轮即可被 claim 重跑。lease_seconds 透传给 reclaim/claim/progress 续租。
    """
    async with maker() as s:
        await reclaim_expired_jobs(s, lease_seconds=lease_seconds)
        job = await claim_next_job(s, worker_id=worker_id, lease_seconds=lease_seconds)
        await s.commit()
    if job is None:
        return False

    async def progress(phase: str, percent: int) -> None:
        async with maker() as s2:
            j = (await s2.execute(select(IndexJob).where(IndexJob.id == job.id))).scalar_one()
            j.status = phase
            j.progress = {"phase": phase, "percent": percent}
            # 每报一次进度续租：把 lease_expires 往后推 lease_seconds，防 reaper 误回收正在跑的作业
            j.lease_expires = datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
            p = (await s2.execute(select(Project).where(Project.id == j.project_id))).scalar_one_or_none()
            if p is not None:
                p.status = "indexing"
                p.indexing_progress = {"phase": phase, "percent": percent}
            await s2.commit()

    try:
        commit_sha = await indexer(job, progress)  # type: ignore[misc]
    except Exception as e:  # noqa: BLE001 — 作业失败要兜住、记 error、决定重试
        async with maker() as s3:
            j = (await s3.execute(select(IndexJob).where(IndexJob.id == job.id))).scalar_one()
            j.attempts += 1
            j.error = str(e)[:2000]
            j.status = FAILED if j.attempts >= MAX_ATTEMPTS else QUEUED
            if j.status == FAILED:
                j.finished_at = datetime.now(timezone.utc)
            j.lease_expires = None  # 终态/重排队都清租约（重 claim 会重置），防 stale lease 留行上
            await s3.commit()
        return True

    async with maker() as s4:
        j = (await s4.execute(select(IndexJob).where(IndexJob.id == job.id))).scalar_one()
        j.status = DONE
        j.commit_sha = commit_sha
        j.finished_at = datetime.now(timezone.utc)
        j.progress = {"phase": DONE, "percent": 100}
        j.lease_expires = None  # 成功终态清租约，避免 done 行留过期 lease 被 reaper 误判

        p = (await s4.execute(select(Project).where(Project.id == j.project_id))).scalar_one_or_none()
        if p is not None:
            p.status = "ready"
            p.last_synced_commit = commit_sha
            p.last_synced_at = datetime.now(timezone.utc)
        await s4.commit()
    return True
