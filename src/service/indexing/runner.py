"""索引作业编排：认领一条 → 跑 indexer → 写进度/终态/重试。设计 §9。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.service.db_models_homepage import IndexJob, Project
from src.service.indexing.queue import claim_next_job
from src.service.indexing.states import DONE, FAILED, QUEUED

MAX_ATTEMPTS = 3

ProgressCb = Callable[[str, int], Awaitable[None]]
IndexerFn = Callable[[IndexJob, ProgressCb], Awaitable[str]]


async def run_one_job(
    maker: async_sessionmaker[AsyncSession], *, worker_id: str, indexer: Optional[IndexerFn],
) -> bool:
    """认领并处理一条作业。返回 True=处理了一条，False=队列空。"""
    async with maker() as s:
        job = await claim_next_job(s, worker_id=worker_id)
        await s.commit()
    if job is None:
        return False

    async def progress(phase: str, percent: int) -> None:
        async with maker() as s2:
            j = (await s2.execute(select(IndexJob).where(IndexJob.id == job.id))).scalar_one()
            j.status = phase
            j.progress = {"phase": phase, "percent": percent}
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
            await s3.commit()
        return True

    async with maker() as s4:
        j = (await s4.execute(select(IndexJob).where(IndexJob.id == job.id))).scalar_one()
        j.status = DONE
        j.commit_sha = commit_sha
        j.finished_at = datetime.now(timezone.utc)
        j.progress = {"phase": DONE, "percent": 100}
        p = (await s4.execute(select(Project).where(Project.id == j.project_id))).scalar_one_or_none()
        if p is not None:
            p.status = "ready"
            p.last_synced_commit = commit_sha
            p.last_synced_at = datetime.now(timezone.utc)
        await s4.commit()
    return True
