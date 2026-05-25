"""todo_write 元工具：模型多步任务自追踪 checklist（设计 §3.3）。

与 8 个 ke_* KB 工具不同：无后端依赖、无副作用——handler 纯回显当前 todo 列表，
让 LLM 知道"已记录"。真正的前端展示靠 sse_emitter 识别这次调用 → 发 `todo`
SSE 事件转发（设计 §8）。所以本工具不查 / 不写任何 store。
"""
from __future__ import annotations

# typing.Any：声明 dict 的值可以是任意类型（JSONSchema 值多样）
from typing import Any

# Tool 是 @dataclass(frozen=True, slots=True)，字段：name/description/input_schema/handler
from src.service.qa_engine.tools.base import Tool

# status 合法值（设计 §3.3）
_TODO_STATUS_ENUM = ["pending", "in_progress", "completed"]

# JSONSchema dict，描述 todo_write 工具的入参结构；被 LLM 用来理解如何调用本工具
_TODO_WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "description": "当前 todo 列表；多步任务时自报进度，简单问题不必调用",
            # items 字段描述数组里每个元素的 schema（嵌套 JSONSchema）
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "任务描述"},
                    "status": {
                        "type": "string",
                        "description": "任务状态",
                        # enum 限定合法值集合
                        "enum": _TODO_STATUS_ENUM,
                    },
                },
                "required": ["content", "status"],
            },
        },
    },
    "required": ["items"],
}


def build_todo_write_tool() -> Tool:
    """构造 todo_write 元工具（无后端依赖，纯状态转发）。

    Returns:
        Tool 实例，名称为 "todo_write"，handler 纯回显 items。
    """

    # handler 是嵌套 async 函数——闭包模式，与 ke_* 工具保持一致
    async def handler(input: dict[str, Any]) -> dict[str, Any]:
        """接收 {items: [...]} 原样返回，兜底非 list 为空列表。

        Args:
            input: 调用入参，期望含 "items" key（list of {content, status}）
        Returns:
            {"items": [...], "count": int}；前端展示靠 sse_emitter 转 `todo` 事件
        """
        # 纯回显：items 原样返回让 LLM 确认"已记录"。前端展示靠 sse_emitter
        # 把这次调用转成 `todo` 事件（设计 §8）。items 缺失/非 list 兜底空列表（不抛，§3.4）。
        items = input.get("items")  # dict.get 找不到 key 时返回 None（不抛 KeyError）
        # isinstance 检查类型：确保 items 是 list，否则兜底空列表（信号哲学：不因格式错误中断流）
        if not isinstance(items, list):
            items = []
        # 返回 items 原值 + count 方便下游快速知道有几条（避免重新 len）
        return {"items": items, "count": len(items)}

    # Tool 是 frozen dataclass，构造后不可变；四个必填字段：name/description/input_schema/handler
    return Tool(
        name="todo_write",
        description=(
            "多步任务自追踪 checklist：把当前待办列表 items（每项 {content, status}，"
            "status ∈ pending/in_progress/completed）记录并展示给用户。"
            "每次调用均为全量快照，替换之前的列表（不要每条 item 调一次）。"
            "仅在多步复杂任务时调用；简单问题无需调用。"
        ),
        input_schema=_TODO_WRITE_SCHEMA,
        handler=handler,
    )
