"""Tool 基础抽象 + ToolRegistry。

为什么这么设计：
  - **MCP 兼容**：input_schema 用 JSONSchema，未来可以无损迁移到外部 MCP server
  - **handler 注入**：业务逻辑放 handler 里，Tool 本身是元数据 + 入口；
    单测可注 mock handler，不必 mock 主仓 Weaviate / Neo4j
  - **async**：所有 handler 都是 async，方便对接 Weaviate v4 / Neo4j 异步 driver
  - **Registry 不做 dispatch 之外的事**（不做缓存、不做重试、不做指标）——
    这些横切关注点交给装饰器 / middleware，v1.2 暂不上
"""
from __future__ import annotations

# dataclasses：自动生成 __init__ / __repr__ / __eq__ / 不可变样板
from dataclasses import dataclass
# typing.Callable + Awaitable：声明 handler 必须是"接收 dict 返回 awaitable 的可调用对象"
# 用 `Any` 简化签名 —— JSONSchema 校验在 v1.3 再上
from typing import Any, Awaitable, Callable


# `ToolHandler` 类型别名：handler 是接收 dict (input)、返回 awaitable dict (output) 的函数
# 等价 of `Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]`
# 用 type alias 让代码可读性更高
ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


# `frozen=True`：Tool 一旦构造完不可改字段（防止 registry 里被偷偷改 schema）
# `slots=True`：用 __slots__ 替代 __dict__，省内存
@dataclass(frozen=True, slots=True)
class Tool:
    """一个工具的元数据 + 异步 handler。

    Attributes:
        name: 工具唯一标识；推荐 'ke_xxx' 前缀方便 MCP server 识别
        description: 自然语言描述；future LLM tool-calling 会读这段来选工具
        input_schema: JSONSchema dict，例如 {"type": "object", "properties": {...}, "required": [...]}
        handler: 异步函数，输入 dict 输出 dict（必须可 JSON 序列化）
    """
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler


class ToolNotFound(LookupError):
    """工具未注册。继承 LookupError 是因为语义上更接近 KeyError，但要更明确。

    `LookupError` 是 `KeyError` 和 `IndexError` 的父类，标准做法。
    """
    pass


class ToolRegistry:
    """工具的"集合 + 调度器"。无状态（除了 self._tools dict），可全局共享。

    用法：
      reg = ToolRegistry()
      reg.register(Tool(...))
      tools = reg.list_tools()           # 给 LLM 看『可用工具是哪些』
      out = await reg.call("ke_callees", {"entity_id": "..."})
    """

    def __init__(self) -> None:
        # `_tools` 用 dict 是因为：1) 按 name 直接 O(1) 查；2) list_tools 时按插入顺序输出
        # Python 3.7+ dict 有序，所以 list(self._tools.values()) 输出顺序 = 注册顺序
        self._tools: dict[str, Tool] = {}

    def list_tools(self) -> list[Tool]:
        """返回所有注册工具，按注册顺序。"""
        # list() 拷贝一份；外部修改不会影响内部状态
        return list(self._tools.values())

    def register(self, tool: Tool) -> None:
        """注册一个工具。重名直接抛 ValueError（编程错误，要早爆）。"""
        # `in` 操作对 dict 是 O(1) 查 key
        if tool.name in self._tools:
            # f-string + match 给测试 / 日志更友好的报错
            raise ValueError(f"工具重名: {tool.name!r} 已注册")
        # 写入 dict；不可变 Tool 实例直接放进去
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        """按 name 查工具。找不到返回 None（仿 dict.get 设计）。"""
        # dict.get(key, default) 找不到时返回 default；这里默认就是 None
        return self._tools.get(name)

    async def call(self, name: str, input: dict[str, Any]) -> dict[str, Any]:
        """调用 name 对应的工具，传入 input dict，await handler 返回。

        Raises:
            ToolNotFound: 工具未注册
        """
        tool = self.get(name)
        # `is None` 比 `not tool` 安全：万一某天 Tool 实例支持 __bool__ 也不会误判
        if tool is None:
            raise ToolNotFound(f"未注册的工具: {name!r}")
        # await handler；handler 必须是 async function
        return await tool.handler(input)
