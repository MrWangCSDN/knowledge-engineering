"""索引作业入队 + 原子认领。设计 §9。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import select, update, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from src.service.db_models_homepage import IndexJob
from src.service.indexing.states import QUEUED, CLONING, DONE, FAILED


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


async def claim_next_job(
    session: AsyncSession, *, worker_id: str, lease_seconds: int = 3600,
) -> Optional[IndexJob]:
    """原子认领最早的 queued 作业 → 置 cloning + worker_id + started_at + lease_expires；抢不到返 None。

    生产 MySQL 并发下应在选 id 的子查询加 FOR UPDATE SKIP LOCKED；SQLite 测试用乐观 rowcount。
    lease_seconds: 租约时长（秒），认领时写入 lease_expires=now+lease_seconds，供 reaper 判过期。
    """
    row = (await session.execute(
        select(IndexJob.id).where(IndexJob.status == QUEUED).order_by(IndexJob.created_at).limit(1)
    )).scalars().first()
    if row is None:
        return None
    now = datetime.now(timezone.utc)
    res = await session.execute(
        update(IndexJob).where(IndexJob.id == row, IndexJob.status == QUEUED)
        .values(status=CLONING, worker_id=worker_id, started_at=now,
                lease_expires=now + timedelta(seconds=lease_seconds))
    )
    await session.flush()
    if res.rowcount != 1:
        return None
    return (await session.execute(select(IndexJob).where(IndexJob.id == row))).scalar_one()


async def reclaim_expired_jobs(
    session: AsyncSession, *, lease_seconds: int = 3600, now: Optional[datetime] = None,
) -> int:
    """回收过期租约的 running 作业（机会式，claim 前调）。返回回收条数。

    running = 非 queued 非终态；过期 = lease_expires<now 或（lease_expires IS NULL 且
    started_at<now-LEASE）。attempts+1 后 >=MAX_ATTEMPTS → FAILED；否则 → QUEUED。
    两条集合 UPDATE 必须 synchronize_session=False（否则默认 evaluate 在已加载 ORM 对象上
    跑 naive<aware 抛 TypeError，范式同 oauth_state_store.gc_expired）。
    """
    from src.service.indexing.runner import MAX_ATTEMPTS   # 延迟 import 防 runner↔queue 环
    if now is None:
        now = datetime.now(timezone.utc)
    started_cutoff = now - timedelta(seconds=lease_seconds)
    running = IndexJob.status.notin_([QUEUED, DONE, FAILED])
    expired = or_(IndexJob.lease_expires < now,
                  and_(IndexJob.lease_expires.is_(None), IndexJob.started_at < started_cutoff))
    base = and_(running, expired)
    r_fail = await session.execute(
        update(IndexJob).where(base, IndexJob.attempts + 1 >= MAX_ATTEMPTS).values(
            status=FAILED, attempts=IndexJob.attempts + 1, finished_at=now, worker_id=None,
            error="reclaimed by reaper: lease expired (max attempts)")
        .execution_options(synchronize_session=False))
    r_queue = await session.execute(
        update(IndexJob).where(base, IndexJob.attempts + 1 < MAX_ATTEMPTS).values(
            status=QUEUED, attempts=IndexJob.attempts + 1, worker_id=None, lease_expires=None)
        .execution_options(synchronize_session=False))
    await session.flush()
    return (r_fail.rowcount or 0) + (r_queue.rowcount or 0)
