"""ReAct 循环里 LLM 来回交流用的数据类型。

不依赖任何 LLM SDK —— 给上层（ReActSynthesizer / DashScope adapter）一个稳定边界。
要换 OpenAI / Claude / Gemini 时只动 provider，不动 synthesizer 和 SSE emitter。
"""
from __future__ import annotations

# `frozen=True` 让实例不可变，hashable，可放 set/dict key
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolCall:
    """LLM 想调一个工具时的请求结构。

    Attributes:
        id:        工具调用的唯一 id（OpenAI 协议里 LLM 自己生成；我们会用它来对结果）
        name:      工具名（必须在 ToolRegistry 里能查到）
        arguments: 入参 dict —— **必须**是已经 JSON 解码好的 dict，不是字符串。
                   provider 负责把 OpenAI 原始字符串 json.loads 后填进来；
                   这样 ReActSynthesizer 不用关心 wire format。
    """
    id: str
    name: str
    # `dict[str, Any]`：Python 3.9+ 内置泛型语法（早期要 `Dict[str, Any]`）
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LLMToolResponse:
    """LLM 一轮返回值。

    Attributes:
        content:    LLM 的文本回答；只有 tool_calls 时（没文本时）可以为 None。
        tool_calls: LLM 想调的工具列表；如果是最终答案就为空 list []。
    """
    # `content: str | None`：3.10+ 的 union type 语法
    content: str | None
    # `field(default_factory=list)`：每个实例独立的默认空列表
    tool_calls: list[ToolCall] = field(default_factory=list)

    def has_tool_calls(self) -> bool:
        """便捷判断：ReAct 循环判 "要不要再转一轮" 时用。"""
        # 列表非空 = True；空列表 = False（Python truthiness）
        return bool(self.tool_calls)


# ─── v1.8 真流式 ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class StreamTextDelta:
    """LLM 流式响应里的一个文本片段（OpenAI delta.content 一次的产出）。

    `complete_stream_with_tools` 在响应过程中可能 yield 多个 StreamTextDelta（文本逐 token 涌出）
    以及零个或多个 ToolCall（每个 tool_calls 都装配完后 yield 一次）。
    """
    text: str


@dataclass(frozen=True, slots=True)
class StreamThinkingDelta:
    """LLM 流式响应里的一段"思考"增量（区别于答案正文 StreamTextDelta）。

    来源：
      - DashScope/qwen：delta.reasoning_content 字段
      - MiniMax-M2：content 里的 <think>...</think> 段（Plan A-cont 处理）

    上层 sse_emitter 按 isinstance 把它路由到 SSE `thinking` 事件 → 前端灰字渲染。
    """
    text: str
