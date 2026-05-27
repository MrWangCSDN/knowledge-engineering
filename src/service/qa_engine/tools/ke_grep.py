"""ke_grep 工具：用 ripgrep 在项目源码中按正则搜索内容。

设计：[[代码源文件查询工具-设计]] §3.1

ripgrep 默认行为保留：自动跳 .gitignore / .git/ / binary / node_modules / target / dist。
subprocess 用列表参数（不 shell=True），shell 注入关死。

依赖：
    ripgrep >= 13.0（系统级安装）：brew install ripgrep
    无需 pip 依赖，纯标准库。
"""
from __future__ import annotations  # 允许类型注解中使用字符串形式（如 `str | None`），Python 3.10 前的向前兼容

# asyncio：Python 标准异步 I/O 库；to_thread() 将同步阻塞调用包成协程，不阻塞事件循环
import asyncio
# json：标准库 JSON 解析；rg --json 输出每行一个 JSON 对象
import json
# subprocess：标准库，用于启动子进程（这里调用外部的 rg 命令）
import subprocess
# Any：typing 模块的类型，表示"任意类型"；用于 dict 值类型注解
from typing import Any

# 从同包的 base 模块导入 Tool 数据类
from src.service.qa_engine.tools.base import Tool


# 模块级常量：单次 grep 超时秒数
# 使用大写命名约定表示常量（Python 惯例，非强制）
GREP_TIMEOUT_SEC = 5

# JSONSchema 定义工具的输入结构
# LLM 工具调用时会按这个 schema 校验并生成参数
_KE_GREP_SCHEMA: dict[str, Any] = {
    "type": "object",  # 根类型为 object（即 Python dict）
    "properties": {
        "pattern": {
            "type": "string",
            "description": "正则表达式（PCRE 风格，如 'RedisTemplate|JedisPool'）",
        },
        "glob": {
            "type": "string",
            "description": "文件名 glob 过滤（默认搜全部文件，例如 '**/*.java' 只搜 Java 文件）",
        },
        "case_sensitive": {
            "type": "boolean",
            "default": False,  # 默认忽略大小写（更实用）
        },
        "max_results": {
            "type": "integer",
            "default": 50,
            "minimum": 1,
            "maximum": 200,  # 防止 LLM 乱传超大值导致响应膨胀
        },
    },
    "required": ["pattern"],  # 只有 pattern 必填，其余都有默认值
}


def build_ke_grep_tool(repo_local_path: str | None) -> Tool:
    """构造一个绑定到 repo_local_path 的 ke_grep Tool。

    使用闭包（closure）绑定路径：handler 内部引用外部函数的 bound_repo 变量，
    即使 handler 在别的地方被调用，bound_repo 始终指向构造时传入的路径。

    Args:
        repo_local_path: 项目源码本地绝对路径（字符串）；
                         传 None 时 handler 会返回 error 而不是抛异常。
    Returns:
        Tool 实例，name="ke_grep"，handler 已绑定路径。
    """
    # 闭包绑定：将外部参数赋值给局部变量，handler 内部捕获的是这个局部变量
    # 等价于把 repo_local_path 的引用"锁住"，防止外部修改后影响 handler 行为
    bound_repo = repo_local_path

    # async def：定义异步函数（协程函数）；调用时返回协程对象，await 才真正执行
    async def handler(input: dict[str, Any]) -> dict[str, Any]:
        """实际处理 grep 请求的协程。

        Args:
            input: LLM 传入的参数 dict，符合 _KE_GREP_SCHEMA 描述
        Returns:
            包含 matches / truncated / total_count 的 dict，
            或包含 error 字段的 dict（遇到错误时）
        """
        # 配置校验：bound_repo 为 None 或空字符串都视为"未配置"
        if not bound_repo:
            # 返回 error dict 而非抛异常：调用方（LLM / ReAct）更容易处理
            return {"error": "source path not configured for this project", "matches": []}

        # input.get("pattern") 取值；None 或缺失时返回 ""；.strip() 去掉首尾空白
        pattern = (input.get("pattern") or "").strip()
        if not pattern:
            return {"error": "missing required field: pattern", "matches": []}

        # 参数解析（带默认值 + 上下限保护）
        glob_filter = (input.get("glob") or "").strip()
        # bool() 将任意值转为 True/False；False 时追加 --ignore-case 给 rg
        case_sensitive = bool(input.get("case_sensitive", False))
        try:
            # int()：强转，防止 LLM 传来 "50"（字符串）而非 50（整数）
            max_results = int(input.get("max_results", 50))
        except (TypeError, ValueError):
            # TypeError：input.get() 返回了不能转 int 的类型（如 dict）
            # ValueError：字符串转 int 失败（如 "abc"）
            max_results = 50
        # max/min 组合限制范围：1 ≤ max_results ≤ 200
        max_results = max(1, min(max_results, 200))

        # 构造 rg 命令（列表形式，不走 shell）
        # 关键安全点：subprocess.run(list, shell=False) 不经过 shell 解析，
        # 即使 pattern 中含 ; && | 等特殊字符也不会被 shell 执行，防止注入攻击
        cmd: list[str] = [
            "rg",           # 可执行文件名
            "--json",       # 输出 JSON 格式（每行一个 JSON 对象）
            "-e", pattern,  # -e：指定正则表达式（分开传，不拼字符串）
        ]
        if not case_sensitive:
            # --ignore-case：rg 的大小写不敏感开关（等价于 grep -i）
            cmd.append("--ignore-case")
        if glob_filter:
            # -g：文件名 glob 过滤；同样分开传，不做字符串拼接
            cmd.extend(["-g", glob_filter])
        # --max-count 1000：单文件最多取 1000 行匹配，防止超大文件撑爆内存
        cmd.extend(["--max-count", "1000", "--"])
        # 最后追加搜索路径；-- 之后的参数视为文件路径，即使路径以 - 开头也安全
        cmd.append(bound_repo)

        try:
            # asyncio.to_thread：将同步的阻塞调用放到线程池执行，不阻塞事件循环
            # 等价于 loop.run_in_executor(None, _run_rg)
            def _run_rg() -> tuple[int, str]:
                """在独立线程中同步运行 rg 子进程。

                Returns:
                    (returncode, stdout) 元组
                """
                proc = subprocess.run(
                    cmd,                 # 命令列表（list 形式，不走 shell）
                    capture_output=True, # 捕获 stdout + stderr（Python 3.7+）
                    text=True,           # stdout/stderr 解码为字符串（UTF-8）
                    timeout=GREP_TIMEOUT_SEC,  # 超时后抛 TimeoutExpired
                )
                # returncode 0 = 有匹配；1 = 无匹配；2 = 错误
                # rg --json 不管有无匹配都往 stdout 写（summary 行），所以这里只用 stdout
                return proc.returncode, proc.stdout

            # await：挂起当前协程，等 to_thread 线程完成后继续
            rc, stdout = await asyncio.to_thread(_run_rg)

        except subprocess.TimeoutExpired:
            # ripgrep 超时（搜索范围太大 or 系统繁忙）
            return {"error": f"ke_grep timeout (>{GREP_TIMEOUT_SEC}s)", "matches": []}
        except FileNotFoundError:
            # rg 可执行文件不在 PATH 中（未安装 ripgrep）
            return {"error": "ripgrep not installed (run: brew install ripgrep)", "matches": []}
        except Exception as e:
            # 兜底：捕获所有其他异常（权限错误、系统调用失败等）
            # type(e).__name__ 取异常类名（如 "PermissionError"）
            return {"error": f"{type(e).__name__}: {e}", "matches": []}

        # 解析 rg --json 输出
        # 格式：每行一个 JSON 对象，type 字段区分：
        #   "begin"   → 开始搜索一个文件
        #   "match"   → 一条匹配行
        #   "end"     → 结束搜索一个文件
        #   "summary" → 搜索汇总（最后一行）
        matches: list[dict] = []  # 收集 type=="match" 的行

        # str.splitlines()：按 \n / \r\n / \r 切行，比 split("\n") 更健壮
        for line in stdout.splitlines():
            if not line.strip():
                continue  # 跳过空行
            try:
                obj = json.loads(line)  # json.loads：将 JSON 字符串解析为 Python dict
            except json.JSONDecodeError:
                continue  # 解析失败则跳过这行（防御性处理）

            # 只关注 type=="match" 的行
            if obj.get("type") != "match":
                continue

            # data 字段包含实际匹配信息
            data = obj.get("data", {})

            # path.text：匹配文件的绝对路径（字符串，如 "/Users/.../Foo.java"）
            abs_path = data.get("path", {}).get("text", "")

            # 将绝对路径转为相对路径（去掉 bound_repo 前缀 + 前导 /）
            rel_path = abs_path
            if abs_path.startswith(bound_repo):
                # abs_path[len(bound_repo):]：切掉前缀；.lstrip("/")：去掉可能残留的 /
                rel_path = abs_path[len(bound_repo):].lstrip("/")

            # line_number：匹配行号（整数，1-based）
            line_num = data.get("line_number", 0)

            # lines.text：匹配行的文本内容（带末尾换行）
            # .rstrip("\n")：去掉末尾换行，保持返回值整洁
            text = data.get("lines", {}).get("text", "").rstrip("\n")

            # 追加到结果列表
            matches.append({
                "path": rel_path,
                "line": line_num,
                "text": text,
            })

            # 达到上限后立即停止解析（不必读完全部输出）
            if len(matches) >= max_results:
                break

        # truncated：结果是否被截断（是否还有更多匹配未返回）
        truncated = len(matches) >= max_results
        return {
            "matches": matches,
            "truncated": truncated,
            "total_count": len(matches),  # 实际返回条数（截断时 ≤ 实际总匹配数）
        }

    # Tool 是 frozen dataclass，构造后不可修改
    return Tool(
        name="ke_grep",
        description=(
            "在当前项目源码中按正则 grep（使用 ripgrep）；"
            "自动跳过 .gitignore 中的文件、二进制文件、.git/ 目录。"
            "项目根路径已由后端绑定，无需提供 path 参数。"
        ),
        input_schema=_KE_GREP_SCHEMA,
        handler=handler,  # 闭包 handler，已捕获 bound_repo
    )
