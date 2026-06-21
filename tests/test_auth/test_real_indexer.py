import os, pytest
from src.service.indexing.real_indexer import build_pipeline_args, make_real_indexer
from src.service.indexing.states import CLONING, BUILDING_GRAPH, INTERPRETING


def test_build_pipeline_args():
    args = build_pipeline_args(repo_dir="/repos/p1", output_dir="/tmp/out")
    assert args[:3] == ["python", "-m", "src.pipeline.cli"]
    assert "/repos/p1" in args
    assert "--with-interpretation" in args
    assert "--output-dir" in args and "/tmp/out" in args


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
