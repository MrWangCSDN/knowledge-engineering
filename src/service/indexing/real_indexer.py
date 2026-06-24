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

# CodegraphFn：给克隆仓建 .codegraph/codegraph.db 的可注入异步函数类型
# 签名为 async (repo_dir: str) -> None；测试可注入 AsyncMock 替身，避免跑真二进制
CodegraphFn = Callable[[str], Awaitable[None]]


async def _default_run_codegraph(repo_dir: str) -> None:
    """默认实现：给克隆仓建 codegraph.db（QA 结构/调用图导航依赖它）。

    index_manager.run_index 是同步 subprocess.run（阻塞），用 asyncio.to_thread
    丢到线程池跑，避免阻塞 worker 的 async 事件循环；run_index 内部 check=True，
    失败会抛 CalledProcessError（fail-fast 由调用方不吞实现）。
    """
    # 函数内 import：run_index 依赖外部 codegraph 二进制，延迟到真正调用时才导入，
    # 也便于测试在 src.integrations.codegraph.index_manager.run_index 上 patch
    from src.integrations.codegraph.index_manager import run_index
    # asyncio.to_thread(fn, *args)（Python 3.9+）把同步阻塞函数 fn 放到默认线程池执行，
    # 返回一个可 await 的协程；这样阻塞的 subprocess.run 不会卡住事件循环
    await asyncio.to_thread(run_index, repo_dir)


def build_pipeline_args(repo_dir: str, output_dir: str, project_id: str) -> list[str]:
    """构造 pipeline CLI 命令（含解读）。

    repo_dir 用 --repo-path 传（不再当 positional，cli.py 的 argparse 没有 positional 参数）；
    project_id 用 --project-id 传，确保每个工程索引自己的代码、用自己的 project_id（scoped clear 才生效）。
    """
    return ["python", "-m", "src.pipeline.cli",
            "--repo-path", repo_dir,
            "--project-id", project_id,
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
                      run_pipeline: RunPipelineFn = _default_run_pipeline,
                      run_codegraph: CodegraphFn = _default_run_codegraph):
    """造一个 IndexerFn：clone → 建 codegraph.db → 跑 pipeline，按 PHASE_ORDER 上报进度。"""
    async def _indexer(job, progress) -> str:
        await progress(CLONING, 5)
        dest = os.path.join(repos_root, job.project_id)
        commit_sha = await provider.clone(installation_id, full_name, ref, subpath, dest)
        await progress(BUILDING_GRAPH, 30)
        # 给克隆仓建 .codegraph/codegraph.db（QA 结构/调用图导航依赖它）。
        # fail-fast：run_codegraph 抛异常时不在此吞掉，向上传播 → runner 的 except 会把作业标 failed + 记 error。
        # 放在 run_pipeline 之前，建库失败即早停（不再跑 pipeline）。
        await run_codegraph(dest)
        out_dir = os.path.join(repos_root, f"{job.project_id}.out")
        await progress(CROSS_SERVICE, 45)
        await progress(EMBEDDING, 60)
        # 透传 job.project_id：让 pipeline 用本工程的 project_id 索引克隆仓（多工程隔离）
        await run_pipeline(build_pipeline_args(dest, out_dir, job.project_id), cwd=None)
        await progress(INTERPRETING, 90)
        return commit_sha
    return _indexer
