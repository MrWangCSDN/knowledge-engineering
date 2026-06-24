import os, pytest
# unittest.mock 提供 mock 工具：AsyncMock 模拟可 await 的协程函数，call/调用顺序可断言
from unittest.mock import AsyncMock, patch
from src.service.indexing.real_indexer import (
    build_pipeline_args, make_real_indexer, _default_run_codegraph,
)
from src.service.indexing.states import CLONING, BUILDING_GRAPH, INTERPRETING


def test_build_pipeline_args():
    # 现在 build_pipeline_args 需要第三个参数 project_id
    args = build_pipeline_args(repo_dir="/repos/p1", output_dir="/tmp/out", project_id="proj-x")
    assert args[:3] == ["python", "-m", "src.pipeline.cli"]
    # 仓库路径必须用 --repo-path 传，且其后紧跟路径值（不再当 positional）
    assert "--repo-path" in args
    assert args[args.index("--repo-path") + 1] == "/repos/p1"
    # project_id 用 --project-id 传，其后紧跟值
    assert "--project-id" in args
    assert args[args.index("--project-id") + 1] == "proj-x"
    # 确认 /repos/p1 不是 positional：紧跟在 "src.pipeline.cli" 后面的不应是它
    assert args[3] != "/repos/p1"
    assert "--with-interpretation" in args
    assert "--output-dir" in args and "/tmp/out" in args


@pytest.mark.asyncio
async def test_make_real_indexer_passes_project_id_to_build_args(monkeypatch, tmp_path):
    # 断言 _indexer 跑通时把 job.project_id 透传进 build_pipeline_args，
    # 从而最终命令含 --project-id <job.project_id>
    captured = {}

    class FakeProvider:
        async def clone(self, installation_id, full_name, ref, subpath, dest):
            return "c" * 40

    async def fake_run_pipeline(args, cwd=None):
        # 捕获最终命令行参数，供后续断言
        captured["args"] = args
        return ""

    indexer = make_real_indexer(
        provider=FakeProvider(), installation_id=7, full_name="o/r", ref="master",
        subpath=None, repos_root=str(tmp_path), run_pipeline=fake_run_pipeline,
        # 注入 no-op codegraph，避免单测真的去跑 codegraph 二进制
        run_codegraph=AsyncMock(),
    )

    class _Job:
        id = "job-y"; project_id = "tenant-42"
    async def progress(phase, percent):
        pass

    await indexer(_Job(), progress)
    args = captured["args"]
    # --project-id 后紧跟 job.project_id
    assert "--project-id" in args
    assert args[args.index("--project-id") + 1] == "tenant-42"
    # 克隆目标目录被当作 --repo-path 传入
    assert "--repo-path" in args


@pytest.mark.asyncio
async def test_make_real_indexer_calls_clone_and_reports(monkeypatch, tmp_path):
    calls = {"cloned": False, "phases": []}

    class FakeProvider:
        async def clone(self, installation_id, full_name, ref, subpath, dest):
            calls["cloned"] = (installation_id, full_name, ref, dest)
            return "b" * 40

    async def fake_run_pipeline(args, cwd=None):
        return ""

    indexer = make_real_indexer(
        provider=FakeProvider(), installation_id=7, full_name="o/r", ref="master",
        subpath=None, repos_root=str(tmp_path), run_pipeline=fake_run_pipeline,
        # 注入 no-op codegraph，避免单测真的去跑 codegraph 二进制
        run_codegraph=AsyncMock(),
    )

    class _Job:
        id = "job-x"; project_id = "p1"
    async def progress(phase, percent):
        calls["phases"].append(phase)

    sha = await indexer(_Job(), progress)
    assert sha == "b" * 40
    assert calls["cloned"][1] == "o/r"
    assert CLONING in calls["phases"]
    assert BUILDING_GRAPH in calls["phases"]
    assert INTERPRETING in calls["phases"]


@pytest.mark.asyncio
async def test_default_run_codegraph_calls_run_index():
    """_default_run_codegraph 应把 repo_dir 透传给 index_manager.run_index（同步 subprocess）。"""
    # patch 在 _default_run_codegraph 实际 import run_index 的模块路径上替换该函数
    # （函数体内是 `from src.integrations.codegraph.index_manager import run_index`，
    #  因此 patch 源模块 src.integrations.codegraph.index_manager.run_index 即可生效）
    with patch("src.integrations.codegraph.index_manager.run_index") as m_run_index:
        await _default_run_codegraph("/x")
    # 断言 run_index 被以 "/x" 调用一次（asyncio.to_thread 会真正在线程里执行被 patch 的 mock）
    m_run_index.assert_called_once_with("/x")


@pytest.mark.asyncio
async def test_make_real_indexer_builds_codegraph_before_pipeline(tmp_path):
    """注入 run_codegraph：应以 dest(=repos_root/project_id) 调用一次，且在 run_pipeline 之前。"""
    # order 记录两个步骤的调用顺序，用于断言 codegraph 先于 pipeline
    order = []

    class FakeProvider:
        async def clone(self, installation_id, full_name, ref, subpath, dest):
            return "b" * 40

    # AsyncMock：可被 await 的 mock，side_effect 在被调用时追加标记到 order
    run_codegraph = AsyncMock(side_effect=lambda d: order.append(("codegraph", d)))

    async def fake_run_pipeline(args, cwd=None):
        order.append(("pipeline", args))
        return ""

    indexer = make_real_indexer(
        provider=FakeProvider(), installation_id=7, full_name="o/r", ref="master",
        subpath=None, repos_root=str(tmp_path),
        run_pipeline=fake_run_pipeline, run_codegraph=run_codegraph,
    )

    class _Job:
        id = "job-x"; project_id = "p1"
    async def progress(phase, percent):
        pass

    await indexer(_Job(), progress)

    # 期望 dest = repos_root/project_id（与 real_indexer 内 clone 目标同公式）
    expected_dest = os.path.join(str(tmp_path), "p1")
    run_codegraph.assert_called_once_with(expected_dest)
    # 调用顺序：codegraph 必须先于 pipeline
    assert [step[0] for step in order] == ["codegraph", "pipeline"]


@pytest.mark.asyncio
async def test_make_real_indexer_fail_fast_propagates_and_skips_pipeline(tmp_path):
    """run_codegraph 抛异常时：_indexer 向上抛（不吞），且 run_pipeline 不被调用（失败早停）。"""
    pipeline_calls = {"n": 0}

    class FakeProvider:
        async def clone(self, installation_id, full_name, ref, subpath, dest):
            return "b" * 40

    # side_effect 为异常类/实例时，AsyncMock 被 await 即抛出
    run_codegraph = AsyncMock(side_effect=RuntimeError("codegraph 建库失败"))

    async def fake_run_pipeline(args, cwd=None):
        pipeline_calls["n"] += 1
        return ""

    indexer = make_real_indexer(
        provider=FakeProvider(), installation_id=7, full_name="o/r", ref="master",
        subpath=None, repos_root=str(tmp_path),
        run_pipeline=fake_run_pipeline, run_codegraph=run_codegraph,
    )

    class _Job:
        id = "job-x"; project_id = "p1"
    async def progress(phase, percent):
        pass

    # 期望异常向上传播（不被 _indexer 吞掉）
    with pytest.raises(RuntimeError, match="codegraph 建库失败"):
        await indexer(_Job(), progress)
    # 失败早停：pipeline 一次都不应被调用
    assert pipeline_calls["n"] == 0
