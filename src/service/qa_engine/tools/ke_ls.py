"""ke_ls 工具：列项目内目录的内容（可递归 1-3 层）。

设计：[[代码源文件查询工具-设计]] §3.4
"""
from __future__ import annotations

# Path：pathlib 的路径抽象，跨平台 + 比 os.path 更面向对象
from pathlib import Path
# Any：当 dict 的 value 类型不固定时用 Any 告诉类型检查器"任意类型都行"
from typing import Any

# Tool：本项目定义的工具基础数据类（name + description + input_schema + handler）
from src.service.qa_engine.tools.base import Tool
# resolve_safe_path：把 LLM 传的 project-relative 路径安全解析为绝对路径（含边界检查）
from src.service.qa_engine.tools._path_sandbox import resolve_safe_path


# JSONSchema 描述 ke_ls 工具接受的入参
# 放模块顶层常量：不随 build_ke_ls_tool() 的每次调用重复创建
_KE_LS_SCHEMA: dict[str, Any] = {
    # "object" 表示入参是一个 JSON 对象（Python dict）
    "type": "object",
    "properties": {
        "path": {
            # 要列出的目录（项目相对路径），默认当前目录 "."
            "type": "string",
            "default": ".",
            "description": "项目相对目录路径",
        },
        "depth": {
            # 递归层数，1 = 只列直接子项，3 = 最多递归 3 层
            "type": "integer",
            "default": 1,
            "minimum": 1,
            "maximum": 3,
        },
        "include_hidden": {
            # 是否包含以 "." 开头的隐藏文件/目录，默认 False
            "type": "boolean",
            "default": False,
        },
    },
}

# 列目录时自动跳过的"噪音"目录名（与 ripgrep 默认过滤对齐）
_SKIP_DIRS = {"node_modules", "target", "dist", ".git"}


def build_ke_ls_tool(repo_local_path: str | None) -> Tool:
    """构造一个绑定到 repo_local_path 的 ke_ls Tool。

    Args:
        repo_local_path: 项目源码本地绝对路径（来自 DB projects.repo_local_path）。
            传 None 表示"未配置"，调用 handler 时会返回 error。

    Returns:
        Tool 实例，可注册到 ToolRegistry 或直接调用 .handler(...)。
    """
    # 闭包捕获：bound_repo 是 repo_local_path 的本地别名，handler 内部通过闭包访问
    # 这是 Python 闭包（closure）的用法：内层函数 handler 可以读取外层函数的局部变量
    bound_repo = repo_local_path

    # async def：声明异步函数，调用时返回 coroutine，需用 await 执行
    async def handler(input: dict[str, Any]) -> dict[str, Any]:
        """ke_ls handler：解析参数 → 沙箱路径检查 → 递归列目录。

        Args:
            input: LLM 传入的参数 dict，可含 path / depth / include_hidden。

        Returns:
            成功：{"path": str, "entries": list[dict]}
            失败：{"error": str, "entries": []}
        """
        # .get("path", ".") or "."：先取 path 字段，空字符串时也退回到 "."
        # .strip()：去掉首尾空白，防 LLM 多传空格
        path = (input.get("path") or ".").strip() or "."

        # resolve_safe_path 做两件事：
        #   1. 检查 bound_repo 不为空（未配置时抛 ValueError）
        #   2. 检查 path 不是绝对路径、不逃出 repo 边界
        # 用 try/except 捕获 ValueError，转成 error dict 返回（不抛异常）
        try:
            target = resolve_safe_path(bound_repo, path)
        except ValueError as e:
            # f-string：将异常消息嵌入字符串；str(e) 调用异常的 __str__ 方法
            return {"error": str(e), "entries": []}

        # 目录不存在 → 返回 error（LLM 传错路径时的友好提示）
        if not target.exists():
            return {"error": f"directory not found: {path}", "entries": []}
        # 路径存在但不是目录 → 也返回 error（ke_ls 只处理目录）
        if not target.is_dir():
            return {"error": f"not a directory: {path}", "entries": []}

        # 解析 depth 参数，做 clamp（限制在 [1, 3] 区间内）
        # int(...) 可能抛 TypeError（None）或 ValueError（"abc"），统一 fallback 到 1
        try:
            depth = int(input.get("depth", 1))
        except (TypeError, ValueError):
            depth = 1
        # max/min 组合实现 clamp：depth = max(1, min(depth, 3))
        depth = max(1, min(depth, 3))

        # bool()：把任意 truthy/falsy 值强转为 True/False
        include_hidden = bool(input.get("include_hidden", False))

        # 用 list 收集所有 entry；通过闭包在 _walk 内部修改（Python 闭包对 list 直接写）
        entries: list[dict] = []
        # bound_repo 可能是 None，但走到这里 resolve_safe_path 已经保证 bound_repo 非空
        # 用 Path() 转为 pathlib Path 对象，便于 .relative_to() 计算相对路径
        root = Path(bound_repo)  # type: ignore[arg-type]

        # 嵌套函数（内层 def）：实现深度优先递归列目录
        # 之所以用嵌套函数而不是模块级函数：需要闭包访问 entries / root / include_hidden / depth
        def _walk(d: Path, current_depth: int) -> None:
            """递归列 d 目录下的所有子项（受 depth 限制）。

            Args:
                d: 当前要列出的目录（绝对路径）。
                current_depth: 当前层号（从 1 开始，root 的直接子项 = 1）。
            """
            # 超过 depth 时直接返回（递归终止条件之一）
            if current_depth > depth:
                return
            # Path.iterdir()：返回目录直接子项的生成器（不递归）
            # sorted()：对 Path 对象排序，默认按字典序（字母序）
            # PermissionError：某些系统目录无读权限，跳过而不是崩溃
            try:
                children = sorted(d.iterdir())
            except PermissionError:
                return
            # for...in：遍历可迭代对象，child 依次是每个子项的 Path 对象
            for child in children:
                # 隐藏文件过滤：名称以 "." 开头 且 未要求包含隐藏文件 → 跳过
                if not include_hidden and child.name.startswith("."):
                    continue
                # 噪音目录过滤：node_modules / target / dist / .git 通常不需要列出
                # `in` 对 set 是 O(1) 查找（比 list 更快）
                if child.name in _SKIP_DIRS:
                    continue
                # relative_to(root)：把绝对路径转成相对于 repo root 的相对路径
                # .as_posix()：用 "/" 分隔符（跨平台一致，Windows 上也输出 "/" ）
                rel = child.relative_to(root).as_posix()
                # 构造 entry dict：type 用 "dir"/"file" 字符串（JSON 友好）
                entry: dict[str, Any] = {
                    "name": rel,
                    # 三元表达式（条件表达式）：x if condition else y
                    "type": "dir" if child.is_dir() else "file",
                }
                # 文件额外记录大小（字节数），目录不记（大小无意义）
                if child.is_file():
                    try:
                        # Path.stat()：返回文件元数据（类似 os.stat()），.st_size 是字节数
                        entry["size"] = child.stat().st_size
                    except OSError:
                        # stat 可能因权限、文件消失等失败，忽略即可
                        pass
                # .append()：把 entry 加到 entries list 末尾
                entries.append(entry)
                # 子项是目录时，递归进入（current_depth + 1 标记层级加深）
                if child.is_dir():
                    _walk(child, current_depth + 1)

        # 从 target（LLM 指定的目录）开始递归，层号从 1 开始
        _walk(target, 1)

        # 返回包含 path 和 entries 的结果 dict
        return {
            "path": path,
            "entries": entries,
        }

    # Tool 是 frozen dataclass：构造后字段不可变，handler 通过闭包绑定了 bound_repo
    return Tool(
        name="ke_ls",
        description="列项目内某个目录的内容；可递归 1-3 层；默认跳隐藏文件 + node_modules/target/dist。",
        input_schema=_KE_LS_SCHEMA,
        handler=handler,
    )
