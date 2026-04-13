"""根据配置加载代码输入源（仓库路径 + 版本 + 模块/文件列表）。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from src.models import CodeInputSource, FileItem, ModuleItem

# Maven/Gradle 编译时可能生成 Java 源码的目录（需要纳入解析）
_GENERATED_SOURCE_DIRS: tuple[str, ...] = (
    "generated-sources",      # Maven 标准: target/generated-sources/
    "generated-test-sources",  # Maven 标准: target/generated-test-sources/
    "generated",              # Gradle 常用: build/generated/
    "apt",                    # 旧版注解处理器
)

# 构建产物目录中应排除的子目录（编译产物、非源码）
_BUILD_ARTIFACT_DIRS: tuple[str, ...] = (
    "classes",            # 编译输出 .class 文件（不应解析）
    "test-classes",       # 测试编译输出
    "maven-status",       # Maven 编译状态
    "maven-archiver",     # Maven 打包
    "surefire-reports",   # 测试报告
    "failsafe-reports",   # 集成测试报告
    "site",               # Maven site 输出
    "dependency",         # Maven dependency:copy 输出
    "antrun",             # Maven Antrun 输出
)


def load_code_source(
    repo_path: str,
    version: str | None = None,
    modules: list[dict] | None = None,
    language: str | None = None,
    include_patterns: list[str] | None = None,
    exclude_dirs: list[str] | None = None,
    include_generated_sources: bool = True,
) -> CodeInputSource:
    """
    构建供结构层消费的代码输入源。

    repo_path: 仓库根目录（本地路径）.
    modules: 如 [{"id": "mall-admin", "business_domains": ["后台管理域"]}, ...].
    include_patterns: 如 ["**/*.java"]，为空则按 language 默认.
    exclude_dirs: 如 ["node_modules", ".git"]（不含 target/build，由智能过滤处理）.
    include_generated_sources: 是否包含 target/generated-sources 下的生成代码（默认 True）.
    """
    repo = Path(repo_path).resolve()
    if not repo.is_dir():
        raise FileNotFoundError(f"repo_path 不存在或非目录: {repo_path}")

    # 基础排除目录（非构建相关）
    base_exclude = exclude_dirs or ["node_modules", ".git", "__pycache__", ".idea", ".vscode"]

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
    generated_count = 0

    for pattern in include_patterns:
        glob_part = pattern.replace("**/", "").lstrip("/")
        for fp in repo.rglob(glob_part):
            if not fp.is_file():
                continue
            try:
                rel = fp.relative_to(repo).as_posix()
            except ValueError:
                continue

            # 路径分段，用于精确目录级过滤（不再用 substring 匹配）
            parts = rel.split("/")

            # 基础排除：精确匹配路径段（而非 substring）
            if any(d in parts for d in base_exclude):
                continue

            # 智能构建目录过滤（target/build 特殊处理）
            if not _should_include_build_path(parts, include_generated_sources):
                continue

            # 推断模块
            module_id = _infer_module_id(rel, module_list)

            # 标记是否为生成代码
            is_generated = _is_generated_source(parts)
            if is_generated:
                generated_count += 1

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
        meta={
            "include_patterns": include_patterns,
            "generated_source_files": generated_count,
            "total_files": len(files),
        },
    )


def _should_include_build_path(parts: list[str], include_generated: bool) -> bool:
    """
    智能判断构建目录下的文件是否应被纳入。

    规则：
    1. target/ 或 build/ 不在路径中 → 纳入
    2. target/generated-sources/**/*.java → 纳入（如果 include_generated=True）
    3. target/classes/ → 排除（编译产物）
    4. target/ 下其他 .java → 排除
    5. build/generated/**/*.java → 纳入（Gradle 生成源码）
    6. build/ 下其他 → 排除

    示例路径:
    ✅ mall-admin/src/main/java/com/Foo.java               (正常源码)
    ✅ mall-admin/target/generated-sources/annotations/com/Foo.java  (注解生成)
    ✅ mall-admin/target/generated-sources/protobuf/com/Foo.java     (Protobuf生成)
    ✅ mall-admin/build/generated/sources/annotationProcessor/Foo.java (Gradle APT)
    ❌ mall-admin/target/classes/com/Foo.java               (编译产物)
    ❌ mall-admin/target/test-classes/com/Foo.java           (测试编译产物)
    ❌ mall-admin/target/maven-status/xxx                    (Maven状态)
    ❌ mall-admin/build/classes/com/Foo.java                 (Gradle编译产物)
    """
    # 找 target 或 build 在路径中的位置
    build_dir_idx = None
    build_dir_name = None
    for i, part in enumerate(parts):
        if part in ("target", "build"):
            build_dir_idx = i
            build_dir_name = part
            break

    # 不在构建目录下 → 正常源码，纳入
    if build_dir_idx is None:
        return True

    # 构建目录下没有子目录 → 排除（如 target/Foo.java）
    remaining = parts[build_dir_idx + 1:]
    if not remaining:
        return False

    # 检查是否在生成源码目录下
    if include_generated:
        for gen_dir in _GENERATED_SOURCE_DIRS:
            if gen_dir in remaining:
                # 确认不在编译产物子目录中
                if not any(art in remaining for art in _BUILD_ARTIFACT_DIRS):
                    return True

    # 检查是否在编译产物目录 → 排除
    if any(art in remaining for art in _BUILD_ARTIFACT_DIRS):
        return False

    # 构建目录下的其他文件 → 排除
    return False


def _is_generated_source(parts: list[str]) -> bool:
    """判断文件是否来自生成源码目录"""
    for i, part in enumerate(parts):
        if part in ("target", "build"):
            remaining = parts[i + 1:]
            return any(gen in remaining for gen in _GENERATED_SOURCE_DIRS)
    return False


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
