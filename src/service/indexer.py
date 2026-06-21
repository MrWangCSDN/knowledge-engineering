"""ke-indexer worker：循环认领并处理索引作业。
入口 `python -m src.service.indexer`（systemd 拉起）。设计 §9。"""
from __future__ import annotations

import asyncio
import os
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.service.indexing.runner import IndexerFn, run_one_job


async def run_worker_loop(
    maker: async_sessionmaker[AsyncSession], *, worker_id: str, indexer: Optional[IndexerFn],
    max_rounds: Optional[int] = None, idle_sleep: float = 2.0,
) -> int:
    """循环处理作业。max_rounds=None 永久跑（生产）；给数字则跑够轮数即停（测试）。
    返回累计处理的作业数。每轮空队列就 sleep(idle_sleep)。"""
    processed = 0
    rounds = 0
    while max_rounds is None or rounds < max_rounds:
        rounds += 1
        handled = await run_one_job(maker, worker_id=worker_id, indexer=indexer)
        if handled:
            processed += 1
        else:
            if max_rounds is not None:
                break
            await asyncio.sleep(idle_sleep)
    return processed


def _main() -> None:  # pragma: no cover — 进程入口
    from src.service.db import get_session_maker
    worker_id = os.getenv("KE_INDEXER_WORKER_ID", "ke-indexer-1")
    # TODO(P3)：真实 indexer 需按 job→project→scm_connection 装配 GitHubAppProvider + installation。
    #          P3 连接 API 落地后在此注入 make_real_indexer；当前入口仅占位，生产启用待 P3。
    raise SystemExit("ke-indexer 入口待 P3 接通真实 indexer 装配（见 TODO）")


if __name__ == "__main__":  # pragma: no cover
    _main()
