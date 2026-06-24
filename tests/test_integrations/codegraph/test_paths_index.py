# tests/test_integrations/codegraph/test_paths_index.py
"""paths + IndexManager 单测：路径拼接确定；init/index 命令构造正确(不真跑 Node)。"""
from src.integrations.codegraph.paths import codegraph_db_path
from src.integrations.codegraph.index_manager import (
    build_index_command, build_init_command, run_index,
)


def test_db_path_under_repo():
    # .codegraph.db 固定在 <repo>/.codegraph/codegraph.db
    assert codegraph_db_path("/repos/mall-swarm") == "/repos/mall-swarm/.codegraph/codegraph.db"


def test_init_command():
    # 构造 `codegraph init <repo>` 命令（克隆仓首次必须 init，否则 index 报未初始化）
    assert build_init_command("/repos/mall-swarm") == ["codegraph", "init", "/repos/mall-swarm"]


def test_index_command():
    # 构造 `codegraph index <repo> [--force]` 命令
    assert build_index_command("/repos/mall-swarm", force=True) == \
        ["codegraph", "index", "/repos/mall-swarm", "--force"]
    assert build_index_command("/repos/mall-swarm", force=False) == \
        ["codegraph", "index", "/repos/mall-swarm"]


def test_run_index_inits_before_indexing(monkeypatch):
    # run_index 必须先 `codegraph init` 再 `codegraph index`（顺序敏感：index 依赖 init）
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        import subprocess
        return subprocess.CompletedProcess(cmd, 0, "", "")

    import src.integrations.codegraph.index_manager as im
    monkeypatch.setattr(im.subprocess, "run", fake_run)

    run_index("/repos/x", force=False)

    assert calls == [
        ["codegraph", "init", "/repos/x"],
        ["codegraph", "index", "/repos/x"],
    ], f"应先 init 后 index，实际: {calls}"


def test_db_path_none_raises():
    # repo_local_path 为空 → 明确 ValueError（而非晦涩 TypeError）
    import pytest
    from src.integrations.codegraph.paths import codegraph_db_path
    with pytest.raises(ValueError):
        codegraph_db_path(None)
    with pytest.raises(ValueError):
        codegraph_db_path("")
