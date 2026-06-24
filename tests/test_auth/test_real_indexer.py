import os, pytest
from src.service.indexing.real_indexer import build_pipeline_args, make_real_indexer
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
