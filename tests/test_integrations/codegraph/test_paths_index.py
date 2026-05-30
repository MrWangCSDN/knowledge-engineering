# tests/test_integrations/codegraph/test_paths_index.py
"""paths + IndexManager 单测：路径拼接确定；index 命令构造正确(不真跑 Node)。"""
from src.integrations.codegraph.paths import codegraph_db_path
from src.integrations.codegraph.index_manager import build_index_command


def test_db_path_under_repo():
    # .codegraph.db 固定在 <repo>/.codegraph/codegraph.db
    assert codegraph_db_path("/repos/mall-swarm") == "/repos/mall-swarm/.codegraph/codegraph.db"


def test_index_command():
    # 构造 `codegraph index <repo> [--force]` 命令
    assert build_index_command("/repos/mall-swarm", force=True) == \
        ["codegraph", "index", "/repos/mall-swarm", "--force"]
    assert build_index_command("/repos/mall-swarm", force=False) == \
        ["codegraph", "index", "/repos/mall-swarm"]
