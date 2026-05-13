"""DashScope（通义千问）LLM provider 适配器。

实现三个接口：
  - LLMProviderProto.complete(system, user) → str            供 QASynthesizer 用
  - ToolCallingLLMProto.complete_with_tools(...)             供 ReActSynthesizer 用（v1.3 起）
  - StreamingLLMProto.complete_stream(system, user) → AsyncIterator[str]  v1.6 token 流

环境变量：
  DASHSCOPE_API_KEY    必填，从阿里云 DashScope 控制台拿
  DASHSCOPE_MODEL      可选，默认 qwen-turbo（便宜）；可选 qwen-plus / qwen-max

参考文档：
  https://help.aliyun.com/zh/dashscope/developer-reference/compatibility-of-openai-with-dashscope
"""
from __future__ import annotations

# json：DashScope tool_calls 里 arguments 是 JSON 字符串，需要 loads 一下
import json
import os
# AsyncIterator：返回值类型注解；fastapi / asyncio 的"惰性流"
from typing import Any, AsyncIterator, Optional

import httpx

from src.service.qa_engine.llm_types import LLMToolResponse, StreamTextDelta, ToolCall


class DashScopeProvider:
    """通义千问 OpenAI 兼容接口的最简 client。

    用法：
        provider = DashScopeProvider()
        answer = await provider.complete(system="...", user="...")
    """

    BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    DEFAULT_MODEL = "qwen-turbo"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 60.0,
    ):
        self.api_key = api_key or os.getenv("DASHSCOPE_API_KEY", "")
        self.model = model or os.getenv("DASHSCOPE_MODEL", self.DEFAULT_MODEL)
        self.timeout = timeout
        if not self.api_key:
            raise RuntimeError(
                "DASHSCOPE_API_KEY 未设置；请在 .env.local 配置或通过 export 注入"
            )

    async def complete(self, *, system: str, user: str, **kwargs: Any) -> str:
        """同步式调用 LLM，返回完整回复字符串。

        v1 不做流式（synthesizer 也是同步式取 raw 输出再解析）。
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(
                f"{self.BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    # 适度温度让回答有"业务说明"的语气，又不至于胡编
                    "temperature": 0.3,
                },
            )
            r.raise_for_status()
            data = r.json()
            return data["choices"][0]["message"]["content"]

    # ─── v1.3 tool calling ────────────────────────────────────────────────

    async def complete_with_tools(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> LLMToolResponse:
        """带 tool calling 的调用；OpenAI 兼容协议。

        :param messages: OpenAI 风格消息列表（含 system / user / assistant / tool 各种 role）
        :param tools: OpenAI tools schema 列表；空列表时 LLM 不会调工具，等价 complete()
        :return: 解析好的 LLMToolResponse（tool_calls 的 arguments 已 json.loads）
        """
        # 组装 payload；tools 字段非空时 DashScope 才走 tool calling 分支
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.3,
        }
        if tools:
            payload["tools"] = tools
            # tool_choice='auto' 表示让模型自己决定调不调工具；默认就是 auto，显式写一下方便排查
            payload["tool_choice"] = "auto"

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(
                f"{self.BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
        # 把 OpenAI 响应 dict 翻译成 LLMToolResponse
        return self._parse_tool_response(data)

    # ─── v1.6 streaming ───────────────────────────────────────────────────

    async def complete_stream(
        self,
        *,
        system: str,
        user: str,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """流式调用 LLM，逐 chunk yield 文本片段。

        OpenAI 兼容协议：
          请求 `"stream": true`
          响应 SSE：每行 `data: {"choices":[{"delta":{"content":"chunk"},...}]}`
          终止：`data: [DONE]`

        :return: AsyncIterator[str]，每个 yield 是一段文本（可能 1 字符也可能 N 字符）
        """
        # httpx 的 `stream("POST", ...)` 返回 async context manager
        # `async with` 自动关闭连接；`aiter_lines()` 按行迭代
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST",
                f"{self.BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.3,
                    "stream": True,
                },
            ) as response:
                response.raise_for_status()
                # `aiter_lines` 异步迭代 HTTP 响应的每一行（按 \n 切）
                async for line in response.aiter_lines():
                    # 静态方法负责把单行解析成 content delta（或 None）
                    delta = self._parse_stream_chunk(line)
                    if delta is not None:
                        yield delta

    @staticmethod
    def _parse_stream_chunk(line: str) -> Optional[str]:
        """从 OpenAI 流式响应的一行里抽 content delta；不可用时返回 None。

        OpenAI 流的行格式：
          `data: {"choices":[{"delta":{"content":"..."},...}],...}`
          `data: [DONE]`  ← 终止信号
          空行 / `:keep-alive` 等心跳行

        :param line: 一行原始 SSE 文本
        :return: content delta 字符串；非 content 行返回 None
        """
        # 空行 / 不以 'data: ' 开头的全部当注释 / 心跳
        if not line or not line.startswith("data: "):
            return None
        payload = line[len("data: "):].strip()
        # OpenAI 终止信号
        if payload == "[DONE]":
            return None
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            # 不合法 JSON 行（比如网络抖动半截数据）：丢掉不抛
            return None
        # choices 必须存在且至少有一条
        choices = obj.get("choices") or []
        if not choices:
            return None
        delta = choices[0].get("delta") or {}
        # `delta.content` 是真正的 token；其它字段（role / function_call / tool_calls）跳过
        content = delta.get("content")
        # OpenAI 协议里 content 可能是 "" 也可能 None；都不算有效 delta
        if not content:
            return None
        # 现实可能给非 str 类型；强转一下安全
        return str(content)

    # ─── v1.8 stream-with-tools ───────────────────────────────────────────

    async def complete_stream_with_tools(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        """流式调用 LLM 并支持 tool calling。

        :yield: 交错出现的 `StreamTextDelta`（文本片段）和 `ToolCall`（装配好的工具调用）
        :note: 单次响应可能 yield 0+ 个 StreamTextDelta 和 0+ 个 ToolCall；
               典型场景是"先吐少量解释文本 → 装配并 yield tool_calls → 结束"

        v1.8：用于 ReAct 模式下首 token 即可见的真流式（替代 v1.7 伪流）。
        """
        # tool_calls 累积器：{index: {id, name, args_buffer}}；
        # 单次响应内累积，结束时清空
        pending: dict[int, dict[str, str]] = {}

        # 组装请求 payload；跟 complete_with_tools 一样的形状，加 stream=True
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.3,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST",
                f"{self.BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
                json=payload,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    events = self._process_stream_line(line, pending)
                    # 每行可能 yield 0、1 或多个事件
                    for ev in events:
                        yield ev

    @staticmethod
    def _process_stream_line(
        line: str,
        pending: dict[int, dict[str, str]],
    ) -> list[Any]:
        """解析 OpenAI 流式响应单行；按需更新 pending（tool_call 累积器）。

        :param line: 一行原始 SSE 文本（不含尾部 \\n）
        :param pending: 状态字典 `{index: {id, name, args_buffer}}`，**会被修改**
        :return: 本行触发的事件列表（StreamTextDelta / ToolCall），空列表表示无事件

        OpenAI tool calling stream 协议要点：
          - delta.content 不空 → 立刻 yield StreamTextDelta
          - delta.tool_calls[i] 第一次出现（含 id / name）→ 写入 pending[i]
          - delta.tool_calls[i].function.arguments → 追加到 pending[i].args_buffer
          - finish_reason='tool_calls' → 装配 pending 里所有 tool_call 并 yield、清空 pending
          - finish_reason='stop' → 仅是普通终止信号，不 yield 任何 tool_call
        """
        # 心跳 / 空行 / 注释 → 跳过
        if not line or not line.startswith("data: "):
            return []
        payload = line[len("data: "):].strip()
        if payload == "[DONE]":
            return []

        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            # 网络抖动半截数据 → 丢掉不抛
            return []

        choices = obj.get("choices") or []
        if not choices:
            return []
        choice = choices[0]
        delta = choice.get("delta") or {}
        finish_reason = choice.get("finish_reason")
        events: list[Any] = []

        # 1) 文本增量 → 立刻 yield
        content = delta.get("content")
        if content:
            events.append(StreamTextDelta(text=str(content)))

        # 2) tool_call 增量 → 累积到 pending
        raw_tool_calls = delta.get("tool_calls") or []
        for tc in raw_tool_calls:
            # OpenAI 用 index 标识"这是第几号 tool_call"（一次响应可能并发多个）
            idx = tc.get("index", 0)
            # 缺省条目（第一次出现这个 index）
            slot = pending.setdefault(idx, {"id": "", "name": "", "args_buffer": ""})
            # id 通常只在第一个 chunk 出现
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["name"] = fn["name"]
            # arguments 是逐 chunk 累加的 JSON 字符串片段
            args_frag = fn.get("arguments")
            if args_frag:
                slot["args_buffer"] = slot.get("args_buffer", "") + args_frag

        # 3) finish_reason='tool_calls' → 装配并 yield 所有 pending tool_call
        if finish_reason == "tool_calls" and pending:
            # 按 index 排序确保顺序稳定（理论上没必要，OpenAI 顺序就是 index 顺序）
            for idx in sorted(pending.keys()):
                slot = pending[idx]
                # 解析 arguments；失败兜底 {}
                try:
                    args = json.loads(slot.get("args_buffer") or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except (json.JSONDecodeError, TypeError):
                    args = {}
                events.append(ToolCall(
                    id=slot.get("id", ""),
                    name=slot.get("name", ""),
                    arguments=args,
                ))
            # 清空（这次响应的 tool_calls 已经全部 yield 完）
            pending.clear()

        return events

    @staticmethod
    def _parse_tool_response(data: dict[str, Any]) -> LLMToolResponse:
        """OpenAI 兼容响应 → LLMToolResponse。

        OpenAI 响应形状（精简）：
            {
              "choices": [{
                "message": {
                  "role": "assistant",
                  "content": "..." | null,
                  "tool_calls": [
                    {"id": "...", "type": "function",
                     "function": {"name": "...", "arguments": "<JSON string>"}}
                  ]?
                }
              }]
            }
        """
        # `choices` 是数组；我们只取第一个（temperature=0.3 + 不开 n=N 就只有一个）
        choices = data.get("choices") or []
        if not choices:
            return LLMToolResponse(content=None, tool_calls=[])
        message = choices[0].get("message", {})

        # content 可能是 None（LLM 调工具时通常没文本）
        content = message.get("content")

        # tool_calls 可能不存在
        raw_calls = message.get("tool_calls") or []
        tool_calls: list[ToolCall] = []
        for raw in raw_calls:
            fn = raw.get("function") or {}
            name = fn.get("name") or ""
            raw_args = fn.get("arguments") or "{}"
            # OpenAI 协议 arguments 是 JSON 字符串；我们解出来变 dict 方便下游用
            # LLM 偶尔会输出不合法 JSON；这种情况兜底空 dict 而不抛错
            try:
                args = json.loads(raw_args)
                # 万一 LLM 返回的是 JSON 数组 / 字符串而非对象（非常少见），强转成 {}
                if not isinstance(args, dict):
                    args = {}
            except (json.JSONDecodeError, TypeError):
                args = {}
            tool_calls.append(ToolCall(id=raw.get("id", ""), name=name, arguments=args))

        return LLMToolResponse(content=content, tool_calls=tool_calls)
