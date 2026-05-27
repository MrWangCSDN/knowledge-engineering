"""ke_glob 工具：按 glob pattern 列项目内匹配文件路径（不读内容）。

设计：[[代码源文件查询工具-设计]] §3.2
"""
# `from __future__ import annotations` 启用 PEP 563 延迟求值注解
# 让 `str | None` 这类 Python 3.10+ 写法在 3.9 也能运行（本项目用 3.12，但保持风格统一）
from __future__ import annotations

# pathlib.Path：面向对象的路径操作库，比 os.path 更直观
# Path.glob(pattern) 支持 ** 递归匹配，如 '**/*.java'
from pathlib import Path

# typing.Any：表示"任意类型"，用于 JSONSchema dict 的 value 类型
from typing import Any

# 从同包的 base 模块导入 Tool dataclass
from src.service.qa_engine.tools.base import Tool


# _KE_GLOB_SCHEMA 是 JSONSchema 格式的工具入参描述
# LLM 会读这份 schema 来决定如何填参数
_KE_GLOB_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            # description 是给 LLM 看的字段说明，越清晰越好
            "description": "glob pattern，如 '**/Mapper/*.xml' 或 'mall-admin/**/*Controller.java'",
        },
        "head": {
            "type": "integer",
            "default": 50,   # 不传时默认返回最多 50 个文件
            "minimum": 1,
            "maximum": 200,  # 防止一次返回过多文件撑爆 context window
        },
    },
    # required 列表：调用时必须提供 pattern
    "required": ["pattern"],
}


def build_ke_glob_tool(repo_local_path: str | None) -> Tool:
    """构造一个绑定到 repo_local_path 的 ke_glob Tool。

    Args:
        repo_local_path: 项目源码在本机的绝对路径，如 '/data/repos/mall-swarm'。
                         传 None 表示尚未配置，调用时会返回 error dict。

    Returns:
        Tool 实例；handler 已通过闭包绑定到 repo_local_path。
    """
    # 用闭包（closure）把 repo_local_path 捕获进 handler 函数里
    # 这样每次调用 handler 时无需重新传入路径，符合"工厂函数"模式
    bound_repo = repo_local_path

    # async def：声明 handler 为协程函数（coroutine function）
    # await 关键字只能在 async def 里使用
    # 这里虽然没有真正 await（Path.glob 是同步的），但保持接口统一
    async def handler(input: dict[str, Any]) -> dict[str, Any]:
        """ke_glob handler：执行 glob 并返回匹配文件列表。

        Args:
            input: 调用参数 dict，字段见 _KE_GLOB_SCHEMA。
                   - pattern (str, required): glob 表达式
                   - head    (int, optional): 最多返回多少个文件，默认 50

        Returns:
            成功：{"files": [...], "truncated": bool, "total_count": int}
            失败：{"error": "...", "files": []}
        """
        # 检查 repo 路径是否已配置
        if not bound_repo:
            # 返回 error dict 而不是抛异常——工具层约定用 dict 传错误
            return {"error": "source path not configured for this project", "files": []}

        # .get() 取字段，返回 None 时用 "" 兜底；.strip() 去掉前后空白
        pattern = (input.get("pattern") or "").strip()
        if not pattern:
            # pattern 为空或全空白，视为缺失必填字段
            return {"error": "missing required field: pattern", "files": []}

        # 解析 head 参数；try/except 防御非整数输入（如 LLM 传 "50" 字符串）
        try:
            # int() 将字符串或浮点数转为整数
            head = int(input.get("head", 50))
        except (TypeError, ValueError):
            # TypeError：input.get 返回 None 时 int(None) 报 TypeError
            # ValueError：int("abc") 报 ValueError
            head = 50

        # max/min 夹拢到合法区间 [1, 200]
        head = max(1, min(head, 200))

        try:
            # Path(str) 将字符串路径包装成 Path 对象
            root = Path(bound_repo)

            # root.glob(pattern) 返回生成器（generator），懒惰求值
            # 生成器：每次 next() 才计算下一个元素，不会一次性把所有结果加载到内存
            # ** 模式需要用 Path.glob（而不是 os.glob），** 表示"任意深度子目录"
            files: list[str] = []
            for p in root.glob(pattern):
                # p.is_file()：排除目录、符号链接指向目录的情况
                if p.is_file():
                    # p.relative_to(root)：把绝对路径转成相对于 root 的路径
                    # .as_posix()：用正斜杠 '/' 分隔，跨平台一致（Windows 也用 /）
                    rel = p.relative_to(root).as_posix()
                    files.append(rel)
                    # 达到 head 上限就停止遍历（生成器未耗尽，节省时间）
                    if len(files) >= head:
                        break

            # 排序：让结果稳定可预测，方便测试断言和用户阅读
            files.sort()

        except Exception as e:
            # 捕获所有异常（如路径不存在、权限拒绝等）
            # type(e).__name__ 取异常类名，如 "FileNotFoundError"
            return {"error": f"{type(e).__name__}: {e}", "files": []}

        return {
            "files": files,
            # len(files) >= head 说明可能还有更多文件未返回
            # `is True/False` 的写法更明确，但这里直接用 bool 表达式也可以
            "truncated": len(files) >= head,
            # total_count 是本次实际返回数量（非磁盘上总数），供调用方参考
            "total_count": len(files),
        }

    # Tool dataclass：name + description + input_schema + handler
    # frozen=True 意味着构造后不可改字段，保证 registry 里的工具是不变的
    return Tool(
        name="ke_glob",
        description="按 glob pattern 列项目内匹配的文件路径（不读内容）；项目根已绑定。",
        input_schema=_KE_GLOB_SCHEMA,
        handler=handler,
    )
