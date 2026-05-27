"""路径沙箱：限制 4 个文件类工具只能访问 project repo_local_path 范围内。

设计：[[代码源文件查询工具-设计]] §4

每个文件工具 handler 第一步都调 resolve_safe_path，把 LLM 传的
project-relative path 拼成绝对路径并做边界检查；拒绝以下情况：
- repo_local_path 为空（DB 字段 NULL）
- relative_path 是绝对路径（防 LLM 传 /etc/passwd）
- relative_path 解析后越出 repo_local_path（含 ../../  和 symlink 逃逸）
"""
from __future__ import annotations

# Path：用 pathlib 而非 os.path，跨平台 + 链式调用更清晰
from pathlib import Path


def resolve_safe_path(repo_local_path: str | None, relative_path: str) -> Path:
    """把 project-relative path 拼成绝对路径并做边界检查。

    :param repo_local_path: 项目源码本地绝对路径（DB projects.repo_local_path）。
        若为 None / 空字符串 → 视作"未配置"，立即抛 ValueError。
    :param relative_path: LLM 传的项目相对路径，如 'mall-admin/pom.xml'。
    :returns: 解析后的绝对路径（已确认在 repo 内）。
    :raises ValueError:
        - repo_local_path 未配置
        - relative_path 是绝对路径
        - 解析后路径越界（含 symlink 逃逸）
    """
    # 1. config sanity：repo 路径必须配置（DB 字段非空）
    if not repo_local_path:
        raise ValueError("source path not configured for this project")

    rel = Path(relative_path)

    # 2. 拒绝绝对路径（防 LLM 传 /etc/passwd 这种）
    # is_absolute() 在 macOS/Linux 上对 '/...' 返回 True
    if rel.is_absolute():
        raise ValueError(f"path must be project-relative, got absolute: {relative_path}")

    # 3. 拼接 + realpath 解析（symlink 也会被跟随到真实目标）
    # Path.resolve() 等同于 os.path.realpath()，处理 ../  / symlink 一并
    root = Path(repo_local_path).resolve()
    target = (root / rel).resolve()

    # 4. 边界检查：解析后路径必须仍以 root 开头
    # relative_to 在不属于 root 时抛 ValueError，我们捕获后换成自己的错误信息
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError(f"path out of repo boundary: {relative_path}")

    return target
