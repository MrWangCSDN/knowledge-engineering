# src/integrations/codegraph/paths.py
"""project → 该工程 .codegraph.db 的路径。

CodeGraph 天生把库放在 <repo>/.codegraph/codegraph.db；多租户即"一工程一库一文件"，
物理隔离，互不打架。
"""
import os  # os 模块提供跨平台的路径、进程等操作系统接口


def codegraph_db_path(repo_local_path: str) -> str:
    """返回该工程 .codegraph.db 的绝对路径。

    Args:
        repo_local_path: 工程根目录的本地绝对路径，例如 "/repos/mall-swarm"
    Returns:
        codegraph.db 文件的完整路径，固定为 <repo>/.codegraph/codegraph.db
    """
    if not repo_local_path:                 # None/空 → 明确报错，别让 os.path.join 抛晦涩 TypeError
        raise ValueError(
            "repo_local_path 为空，无法定位 CodeGraph 库（该工程可能未配置 repo_local_path）"
        )
    # os.path.join 按系统分隔符拼路径
    return os.path.join(repo_local_path, ".codegraph", "codegraph.db")
