"""根据配置加载代码输入源（仓库路径 + 版本 + 模块/文件列表）。"""
from __future__ import annotations

import os
from pathlib import Path

from src.models import CodeInputSource, FileItem, ModuleItem


def load_code_source(
    repo_path: str,
    version: str | None = None,
    modules: list[dict] | None = None,
    language: str | None = None,
    include_patterns: list[str] | None = None,
    exclude_dirs: list[str] | None = None,
) -> CodeInputSource:
    """
    构建供结构层消费的代码输入源。
    repo_path: 仓库根目录（本地路径）.
    modules: 如 [{"id": "mall-admin", "business_domains": ["后台管理域"]}, ...].
    include_patterns: 如 ["**/*.java"]，为空则按 language 默认.
    exclude_dirs: 如 ["target", "node_modules", ".git"].
    """
    repo = Path(repo_path).resolve()
    if not repo.is_dir():
        raise FileNotFoundError(f"repo_path 不存在或非目录: {repo_path}")

    exclude_dirs = exclude_dirs or ["target", "build", "node_modules", ".git", "__pycache__", ".idea"]
    if language == "java" and include_patterns is None:
        include_patterns = ["**/*.java"]
    elif include_patterns is None:
        include_patterns = ["**/*.py", "**/*.java", "**/*.ts", "**/*.js"]

    module_list: list[ModuleItem] = []
    if modules:
        for m in modules:
            mid = m.get("id") or m.get("name", "")
            module_list.append(
                ModuleItem(
                    id=mid,
                    name=m.get("name"),
                    path=m.get("path"),
                    business_domains=m.get("business_domains") or [],
                )
            )

    files: list[FileItem] = []
    for pattern in include_patterns:
        glob_part = pattern.replace("**/", "").lstrip("/")  # **/*.java -> *.java
        for fp in repo.rglob(glob_part):
            if not fp.is_file():
                continue
            try:
                rel = fp.relative_to(repo).as_posix()
            except ValueError:
                continue
            if any(rel.startswith(d) or d in rel for d in exclude_dirs):
                continue
            # 根据路径推断模块（Maven 多模块：第一级目录多为模块名）
            module_id = _infer_module_id(rel, module_list)
            files.append(
                FileItem(
                    path=rel,
                    module_id=module_id,
                    language=language or _guess_language(fp.suffix),
                )
            )

    return CodeInputSource(
        repo_path=str(repo),
        version=version,
        modules=module_list,
        files=files,
        language=language,
        meta={"include_patterns": include_patterns},
    )


def _infer_module_id(relative_path: str, modules: list[ModuleItem]) -> str | None:
    """从相对路径推断所属模块（如 mall-admin/xxx => mall-admin）；单模块时全部归该模块。"""
    parts = relative_path.replace("\\", "/").split("/")
    if not parts:
        return None
    first = parts[0]
    for m in modules:
        if m.id == first or (m.path and relative_path.startswith(m.path)):
            return m.id
    # 仅配置一个模块时，视为单模块仓库，所有文件归该模块
    if len(modules) == 1:
        return modules[0].id
    return first if modules else None


def _guess_language(suffix: str) -> str:
    m = {".java": "java", ".py": "python", ".ts": "typescript", ".js": "javascript"}
    return m.get(suffix.lower(), "unknown")
