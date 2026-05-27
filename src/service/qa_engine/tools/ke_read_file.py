"""ke_read_file 工具：读项目内文件，按 line range 返回。

设计：[[代码源文件查询工具-设计]] §3.3

依赖：
    无额外 pip 依赖，纯标准库（pathlib / typing）。
"""
from __future__ import annotations  # 允许类型注解中使用 `str | None` 写法（Python 3.10 前的向前兼容）

# pathlib.Path：面向对象的路径操作，比 os.path 更清晰
from pathlib import Path
# Any：typing 模块的类型，表示"任意类型"，用于 dict 值的类型注解
from typing import Any

# 从同包的 base 模块导入 Tool 数据类（封装元数据 + handler）
from src.service.qa_engine.tools.base import Tool
# resolve_safe_path：sandbox 路径检查工具——限制文件访问在 repo_local_path 范围内
from src.service.qa_engine.tools._path_sandbox import resolve_safe_path


# 单文件最大读取字节数：1MB
# 防止 LLM 传入一个几十 MB 的巨大文件把响应撑爆
MAX_FILE_SIZE_BYTES = 1024 * 1024  # 1MB = 1024 * 1024 bytes


# _KE_READ_FILE_SCHEMA：工具的输入 JSONSchema，符合 MCP / LLM 工具调用规范
# 用 dict[str, Any] 类型注解：这是一个字典，键是字符串，值类型任意
_KE_READ_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",  # 根类型是 object（对应 Python dict）
    "properties": {
        "path": {
            "type": "string",
            # description 字段给 LLM 读，让 LLM 知道该传什么
            "description": "项目相对路径，如 'mall-admin/pom.xml'",
        },
        "offset": {
            "type": "integer",
            "default": 0,       # 默认从第 0 行开始（0-based）
            "minimum": 0,       # 不允许负数
            "description": "起始 line（0-based）",
        },
        "limit": {
            "type": "integer",
            "default": 200,     # 默认最多返回 200 行
            "minimum": 1,       # 至少读 1 行
            "maximum": 1000,    # 最多 1000 行（防止 LLM 乱传超大值）
        },
    },
    # required：只有 path 是必须的；offset/limit 有默认值，不传也行
    "required": ["path"],
}


def _looks_binary(content_bytes: bytes) -> bool:
    """启发式判断是否为二进制文件：前 8KB 中出现 NUL byte（0x00）则认为是 binary。

    Args:
        content_bytes: 文件的原始字节内容
    Returns:
        True = 二进制文件（不应用文本方式读取）；False = 可能是文本文件
    Note:
        NUL byte（b"\\x00"）在纯文本文件中极少出现，在 ELF/class/zip/png 等
        二进制格式的前几百字节里必然出现，因此是一个可靠的启发式信号。
        与 git 的 binary detection 逻辑相同。
    """
    # b"\x00"：字节字面量，单个 NUL byte（0x00）
    # content_bytes[:8192]：只检查前 8KB（8192 = 8 * 1024），避免读大文件
    # in 操作符对 bytes 类型检查子序列是否存在，O(n) 扫描
    return b"\x00" in content_bytes[:8192]


def build_ke_read_file_tool(repo_local_path: str | None) -> Tool:
    """构造一个绑定到 repo_local_path 的 ke_read_file Tool。

    使用闭包绑定路径：handler 内部引用外部的 bound_repo 变量，
    使得无论 handler 在哪里被调用，都始终使用构造时传入的路径。

    Args:
        repo_local_path: 项目源码本地绝对路径（字符串）；
                         传 None 时 handler 会返回 error dict 而非抛异常。
    Returns:
        Tool 实例，name="ke_read_file"，handler 已绑定路径。
    """
    # 闭包绑定：将参数赋值给局部变量，handler 内部"捕获"这个局部变量
    # 这样即使外部再次调用 build_ke_read_file_tool(另一个路径)，
    # 之前返回的 handler 里的 bound_repo 不会被修改
    bound_repo = repo_local_path

    # async def：定义异步协程函数；调用时返回协程对象，await 后才真正执行
    async def handler(input: dict[str, Any]) -> dict[str, Any]:
        """读取项目内某个文件的内容，按 line range 返回。

        Args:
            input: LLM 传入的参数 dict，符合 _KE_READ_FILE_SCHEMA 描述：
                   - path (str, 必须): 项目相对路径
                   - offset (int, 可选): 起始行号（0-based），默认 0
                   - limit (int, 可选): 最多返回行数，默认 200，上限 1000
        Returns:
            成功时返回包含以下字段的 dict：
                - path: 请求的相对路径
                - content: 选中行的文本内容（以 \\n 连接）
                - line_start: 实际起始行号（0-based）
                - line_end: 实际结束行号（含）（0-based）
                - eof: 是否已到达文件末尾
                - total_lines: 文件总行数
            出错时返回 {"error": "<原因描述>"} dict
        """
        # 取 path 字段，strip() 去掉首尾空格（防 LLM 传多余空格）
        path = (input.get("path") or "").strip()
        # 缺 path 字段直接报错（必填字段）
        if not path:
            return {"error": "missing required field: path"}

        # 用 resolve_safe_path 做 sandbox 检查：
        # - bound_repo 为 None/空 → ValueError("not configured")
        # - path 是绝对路径    → ValueError("absolute")
        # - path 越界（../..） → ValueError("boundary")
        # ValueError 一律转为 error dict，不向上抛异常
        try:
            target = resolve_safe_path(bound_repo, path)
        except ValueError as e:
            # str(e)：将 ValueError 的错误消息转为字符串
            return {"error": str(e)}

        # 文件存在性检查
        if not target.exists():
            return {"error": f"file not found: {path}"}

        # 类型检查：拒绝目录、socket、设备文件等非普通文件
        if not target.is_file():
            return {"error": f"not a regular file: {path}"}

        # 文件大小检查：超 1MB 则拒绝（防止把巨大 log/dump 塞进 LLM 上下文）
        # stat().st_size：文件字节数，不读内容就能拿到
        size = target.stat().st_size
        if size > MAX_FILE_SIZE_BYTES:
            # 错误信息包含实际大小，方便调试
            return {"error": f"file too large ({size} bytes > {MAX_FILE_SIZE_BYTES} max)"}

        # 读取原始字节（bytes）——先读 bytes 才能做 binary 检测
        try:
            raw = target.read_bytes()
        except Exception as e:
            # 兜底异常（权限错误等）
            # type(e).__name__：拿到异常的类名（如 "PermissionError"）
            return {"error": f"read failed: {type(e).__name__}: {e}"}

        # 二进制文件检测：前 8KB 有 NUL byte → 拒读
        if _looks_binary(raw):
            return {"error": "binary file not supported"}

        # UTF-8 解码：errors="replace" 表示遇到无效字节时用 U+FFFD 替代，
        # 而不是抛 UnicodeDecodeError（容错处理）
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            # 理论上 _looks_binary 已过滤掉大部分非 UTF-8 文件，
            # 但 Latin-1 / GBK 等编码文件没有 NUL byte 也可能通过；
            # 用 errors="replace" 降级处理，保证不崩
            text = raw.decode("utf-8", errors="replace")

        # 解析 offset 参数（起始行号，0-based）
        try:
            # int()：强制转换，防止 LLM 传字符串 "10" 而非整数 10
            offset = max(0, int(input.get("offset", 0)))
        except (TypeError, ValueError):
            # TypeError: input.get() 返回了 None 或非数字类型
            # ValueError: 字符串转整数失败（如 "abc"）
            offset = 0

        # 解析 limit 参数（最多返回行数）
        try:
            limit = int(input.get("limit", 200))
        except (TypeError, ValueError):
            limit = 200
        # max/min 组合：确保 1 ≤ limit ≤ 1000
        limit = max(1, min(limit, 1000))

        # 按行切片
        # splitlines()：按 \n / \r\n / \r 分行（比 split("\n") 更健壮）
        all_lines = text.splitlines()
        # len()：列表长度 = 总行数
        total_lines = len(all_lines)

        # 计算切片结束索引（不含）；min 防止越界
        end_idx = min(offset + limit, total_lines)

        # list[start:end]：Python 切片语法，取 [offset, end_idx) 范围的元素
        selected = all_lines[offset:end_idx]

        # "\n".join(iterable)：将列表各元素用 \n 连接成字符串
        content = "\n".join(selected)

        # 构造返回 dict
        return {
            "path": path,
            "content": content,
            "line_start": offset,
            # line_end：最后一行的 0-based 行号（含）
            # 若 end_idx > offset 表示确实读到了内容；否则返回 offset（空切片时的安全值）
            "line_end": end_idx - 1 if end_idx > offset else offset,
            # eof：当读到的末尾 >= 文件总行数时，表示已到达文件末尾
            "eof": end_idx >= total_lines,
            "total_lines": total_lines,
        }

    # 返回 Tool 实例（frozen dataclass，构造后字段不可修改）
    return Tool(
        name="ke_read_file",
        description=(
            "读项目内某个文件的内容；path 是项目相对路径，"
            "按 line range（offset/limit）返回内容。"
            "支持 1MB 以内的文本文件；自动拒绝二进制文件。"
        ),
        input_schema=_KE_READ_FILE_SCHEMA,
        # handler 是闭包函数，已捕获 bound_repo；传给 Tool 后就绑死了
        handler=handler,
    )
