"""默认真实 indexer：clone(P1) + shell 出 pipeline。设计 §9。
为可测，clone/pipeline 均可注入；progress 上报覆盖 PHASE_ORDER。"""
from __future__ import annotations

import asyncio
import os
import subprocess
from typing import Awaitable, Callable, Optional

from src.service.indexing.states import (
    CLONING, BUILDING_GRAPH, CROSS_SERVICE, EMBEDDING, INTERPRETING,
)

RunPipelineFn = Callable[..., Awaitable[str]]


def build_pipeline_args(repo_dir: str, output_dir: str) -> list[str]:
    """构造 pipeline CLI 命令（含解读）。"""
    return ["python", "-m", "src.pipeline.cli", repo_dir,
            "--with-interpretation", "--output-dir", output_dir]


async def _default_run_pipeline(args: list[str], cwd: Optional[str] = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"pipeline 失败(rc={proc.returncode}): {err.decode('utf-8','replace')[:2000]}")
    return out.decode("utf-8", "replace")


def make_real_indexer(*, provider, installation_id: int, full_name: str, ref: str,
                      subpath: Optional[str], repos_root: str,
                      run_pipeline: RunPipelineFn = _default_run_pipeline):
    """造一个 IndexerFn：clone → 跑 pipeline，按 PHASE_ORDER 上报进度。"""
    async def _indexer(job, progress) -> str:
        await progress(CLONING, 5)
        dest = os.path.join(repos_root, job.project_id)
        commit_sha = await provider.clone(installation_id, full_name, ref, subpath, dest)
        await progress(BUILDING_GRAPH, 30)
        out_dir = os.path.join(repos_root, f"{job.project_id}.out")
        await progress(CROSS_SERVICE, 45)
        await progress(EMBEDDING, 60)
        await run_pipeline(build_pipeline_args(dest, out_dir), cwd=None)
        await progress(INTERPRETING, 90)
        return commit_sha
    return _indexer
