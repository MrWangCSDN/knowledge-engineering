# src/integrations/codegraph/index_manager.py
"""跑 `codegraph index` 给某工程建/更新 .codegraph.db（子进程）。"""
from __future__ import annotations  # 允许在类型注解里用 str 引用自身类（向前引用），Python 3.10+ 内置，这里为 3.9 兼容

import subprocess              # subprocess 模块：在 Python 中启动并管理外部子进程
from typing import Optional    # Optional[X] 等价于 Union[X, None]，表示该参数可以是 X 类型或 None


def build_index_command(repo_local_path: str, force: bool = False) -> list[str]:
    """构造 codegraph index 命令（纯函数，便于单测）。

    纯函数意味着：相同输入必然产生相同输出，无副作用（不写文件、不跑进程）。
    这样可以在单测中直接断言返回列表，而不需要真正启动 Node.js 进程。

    Args:
        repo_local_path: 工程根目录本地绝对路径，例如 "/repos/mall-swarm"
        force: True 时追加 --force 标志，让 CodeGraph 全量重建索引（忽略缓存）
    Returns:
        可直接传给 subprocess.run 的命令列表，例如：
        ["codegraph", "index", "/repos/mall-swarm", "--force"]
    """
    # 基础命令列表：可执行文件名 + 子命令 + 目标路径
    # subprocess 接收列表而非字符串，可避免 shell 注入风险
    cmd = ["codegraph", "index", repo_local_path]

    if force:                        # force=True 时进行全量重建（覆盖增量缓存）
        cmd.append("--force")        # list.append 在末尾追加一个元素，原地修改列表

    return cmd  # 返回构造好的命令列表


def run_index(repo_local_path: str, force: bool = False,
              timeout: Optional[int] = 600) -> subprocess.CompletedProcess:
    """跑索引；冷库 ~1-2min，热重建 ~20s。timeout 默认 10min。

    Args:
        repo_local_path: 工程根目录本地绝对路径
        force: True 时全量重建，False 时增量更新
        timeout: 超时秒数，超过后抛 subprocess.TimeoutExpired；None 表示不限时
    Returns:
        subprocess.CompletedProcess 实例，包含 returncode / stdout / stderr
    Raises:
        subprocess.CalledProcessError: 进程退出码非 0 时（check=True 触发）
        subprocess.TimeoutExpired: 超时时
    """
    # subprocess.run 同步等待子进程结束后返回
    # capture_output=True：把子进程的 stdout/stderr 捕获到返回对象，而不是直接打印到终端
    # text=True：把 stdout/stderr 解码为 str（默认是 bytes）
    # timeout：超时秒数，超过则抛 TimeoutExpired 并终止子进程
    # check=True：若退出码 != 0，自动抛 CalledProcessError，上层可感知失败而非静默忽略
    return subprocess.run(
        build_index_command(repo_local_path, force),  # 调用纯函数构造命令列表
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
