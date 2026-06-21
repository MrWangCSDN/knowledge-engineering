"""索引作业入队 + 原子认领。设计 §9。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.service.db_models_homepage import IndexJob
from src.service.indexing.states import QUEUED, CLONING


async def enqueue_index_job(
    session: AsyncSession, *, project_id: str, type_: str, trigger: str,
    dedup_key: Optional[str] = None, commit_sha: Optional[str] = None,
) -> IndexJob:
    """入队一条作业。dedup_key 命中未终态的同 key 作业则返回已有（webhook 去重）。"""
    if dedup_key:
        existing = (await session.execute(
            select(IndexJob).where(IndexJob.dedup_key == dedup_key)
            .where(IndexJob.status.notin_(["done", "failed"]))
        )).scalars().first()
        if existing is not None:
            return existing
    job = IndexJob(
        id=f"job-{uuid.uuid4().hex[:16]}", project_id=project_id, type=type_,
        status=QUEUED, trigger=trigger, dedup_key=dedup_key, commit_sha=commit_sha,
    )
    session.add(job)
    await session.flush()
    return job


async def claim_next_job(session: AsyncSession, *, worker_id: str) -> Optional[IndexJob]:
    """原子认领最早的 queued 作业 → 置 cloning + worker_id + started_at；抢不到返 None。

    生产 MySQL 并发下应在选 id 的子查询加 FOR UPDATE SKIP LOCKED；SQLite 测试用乐观 rowcount。
    """
    row = (await session.execute(
        select(IndexJob.id).where(IndexJob.status == QUEUED).order_by(IndexJob.created_at).limit(1)
    )).scalars().first()
    if row is None:
        return None
    now = datetime.now(timezone.utc)
    res = await session.execute(
        update(IndexJob).where(IndexJob.id == row, IndexJob.status == QUEUED)
        .values(status=CLONING, worker_id=worker_id, started_at=now)
    )
    await session.flush()
    if res.rowcount != 1:
        return None
    return (await session.execute(select(IndexJob).where(IndexJob.id == row))).scalar_one()
