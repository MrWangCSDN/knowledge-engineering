"""验证 ToolRegistry：MCP-style 工具注册 + 查找 + 调用。

设计原则（[[首页设计]] §12 → v1.2）：
  - 每个 Tool = {name, description, input_schema (JSONSchema), handler(async)}
  - Registry 管理所有 Tool 的生命周期
  - 重名注册视为编程错误，要早爆
  - 调用未知工具抛 ToolNotFound（让上层决定要不要降级 / 兜底）
  - 未来对接外部 MCP server 时，handler 替换成 RPC 调用即可，Registry 接口不变
"""
# pytest：标准测试框架
import pytest

# 待实现的类；现在 import 必失败 → 第一个 RED 自然产生
# `ToolRegistry` 管理工具集合；`Tool` 是工具元数据 + handler 的容器
from src.service.qa_engine.tools.base import Tool, ToolRegistry


def test_new_registry_has_no_tools() -> None:
    """新建的 ToolRegistry 里没有任何工具。"""
    registry = ToolRegistry()
    # list_tools() 应该返回空列表
    assert registry.list_tools() == []


# ───────── RED 2: register + get by name ─────────


# 用 pytest_asyncio.fixture 不行，pytest 自带 fixture 也能
# 但 handler 是 async function，需要在 async test 里 await，所以 test 本身要 @pytest.mark.asyncio
async def _echo_handler(input: dict) -> dict:
    """测试用的最简 handler：原样返回输入。

    用 `async def` 是因为 Tool.handler 协议要求"返回 awaitable"；
    这是 Python "async function 自动产生 coroutine（awaitable）"的便利特性。
    """
    return {"echo": input}


def _make_test_tool(name: str = "test_tool") -> Tool:
    """工厂函数：造一个最简 Tool 实例，给多个测试复用。"""
    return Tool(
        name=name,
        description=f"测试用 tool: {name}",
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=_echo_handler,
    )


def test_register_and_get_tool_by_name() -> None:
    """注册 → 按 name 查回来。"""
    registry = ToolRegistry()
    tool = _make_test_tool("my_tool")
    registry.register(tool)

    # `get` 返回 Tool 实例；找不到时返回 None（参考 dict.get 设计）
    fetched = registry.get("my_tool")
    assert fetched is tool  # 同一个对象，不只是值相等

    # list_tools 也应该能看见
    assert registry.list_tools() == [tool]


def test_get_unknown_tool_returns_none() -> None:
    """get 找不到工具时返回 None，不抛错（让调用方决定要不要兜底）。"""
    registry = ToolRegistry()
    assert registry.get("nonexistent") is None


# ───────── RED 3: 重名注册抛错 ─────────


def test_register_duplicate_name_raises_value_error() -> None:
    """同名注册两次视为编程错误，直接 ValueError 早爆。

    选 ValueError 而非自定义异常：这是不可恢复的配置错误，不需要细粒度异常类。
    """
    registry = ToolRegistry()
    registry.register(_make_test_tool("dup"))
    # `pytest.raises` 上下文管理器：断言 with 块内必须抛指定异常
    with pytest.raises(ValueError, match="dup"):
        registry.register(_make_test_tool("dup"))


# ───────── RED 4: call() 调用 + 未知工具抛 ToolNotFound ─────────


@pytest.mark.asyncio
async def test_call_dispatches_to_registered_handler() -> None:
    """call(name, input) 应该把 input 喂给 handler 并 await 它的返回。"""
    registry = ToolRegistry()
    registry.register(_make_test_tool("echo"))
    # `await` 异步调用；handler 用的就是 _echo_handler
    result = await registry.call("echo", {"hello": "world"})
    # _echo_handler 把 input 包在 {"echo": ...} 里返回
    assert result == {"echo": {"hello": "world"}}


@pytest.mark.asyncio
async def test_call_unknown_tool_raises_tool_not_found() -> None:
    """call 找不到工具时抛 ToolNotFound（继承 LookupError）。"""
    # 局部 import 避免 collection 时模块依赖循环
    from src.service.qa_engine.tools.base import ToolNotFound

    registry = ToolRegistry()
    with pytest.raises(ToolNotFound, match="nonexistent"):
        await registry.call("nonexistent", {})
