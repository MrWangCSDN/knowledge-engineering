"""ReAct 风格的 synthesizer：让 LLM 主动调 tool，多轮迭代后给最终答案。

跟 QASynthesizer 的区别：
  - QASynthesizer：一次性 system + user prompt → LLM 一轮 → 解析 sections
  - ReActSynthesizer：LLM 可能要求"先调 ke_callees(M1)"，结果喂回后再问下一轮，
                     直到 LLM 决定输出最终 JSON sections

外部接口跟 QASynthesizer 兼容（都返回 SynthesizedAnswer），方便 sse_emitter 切换。

设计文档：[[首页设计]] §13 v1.3
"""
from __future__ import annotations

# json：把 tool 结果序列化成字符串喂回 LLM（OpenAI 协议要求 tool message content 是 string）
import asyncio
import json
# typing.Any 是"我不想标具体类型"的占位
from typing import Any, Awaitable, Callable, Optional, Protocol

from src.service.qa_engine.llm_types import LLMToolResponse, StreamTextDelta, StreamThinkingDelta, ToolCall
from src.service.qa_engine.retriever import RetrievedContext
from src.service.qa_engine.synthesizer import SynthesizedAnswer, QASynthesizer
from src.service.qa_engine.tools.base import Tool, ToolNotFound, ToolRegistry


# `Protocol` 定义"LLM provider 必须有的方法"，让 ReActSynthesizer 跟具体厂商解耦
class ToolCallingLLMProto(Protocol):
    """支持 tool calling 的 LLM provider 接口。

    实现可以是 DashScope / OpenAI / Claude / Gemini —— 反正都封装成这个签名。
    """

    async def complete_with_tools(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LLMToolResponse:
        """发起一轮带 tool calling 的 LLM 调用。

        :param messages: OpenAI-style 消息列表（含 system / user / assistant / tool 各种 role）
        :param tools:    OpenAI-style 工具 schema 列表
        :return: 该轮 LLM 输出（含可能的 tool_calls）
        """
        ...


class ReActSynthesizer:
    """LLM tool-calling 编排器。

    用法跟 QASynthesizer 一致：
      synth = ReActSynthesizer(llm_provider=llm, tool_registry=reg)
      answer = await synth.synthesize(ctx)
    """

    def __init__(
        self,
        *,
        llm_provider: ToolCallingLLMProto,
        tool_registry: ToolRegistry,
        max_iterations: int = 12,
    ) -> None:
        """
        :param llm_provider: 实现 ToolCallingLLMProto 的实例
        :param tool_registry: 已经注册好的 ToolRegistry（含 5 个 ke_* 之类）
        :param max_iterations: ReAct 循环最多跑几轮；防止 LLM 死循环调工具
        """
        self.llm = llm_provider
        self.tool_registry = tool_registry
        # 上限保护：12 轮安全阀，支撑多跳调用链/影响分析跑到收敛；
        # 停止靠"模型给最终答案（无 tool_calls）即 return"，正常远不到 12
        self.max_iterations = max_iterations

    async def synthesize(
        self,
        ctx: RetrievedContext,
        history: list[dict[str, Any]] | None = None,
        on_tool_call: Optional[Callable[..., Awaitable[None]]] = None,
    ) -> SynthesizedAnswer:
        """主入口：跑 ReAct 循环，返回 6 段式答案。

        :param on_tool_call: 可选的异步回调；每次 tool 执行前后会被调用：
            await on_tool_call("starting", call)              # 开始前
            await on_tool_call("complete", call, result_dict) # 完成后
            sse_emitter 用它实时发"event: tool_call"事件给前端。
        """
        from src.service.qa_engine.prompts import SYSTEM_PROMPT, build_user_prompt
        from src.service.qa_engine.synthesizer import _ctx_to_dict

        user_prompt = build_user_prompt(ctx.question, _ctx_to_dict(ctx))
        # 给 system prompt 加 tool 使用指引（只有注册了工具才加，避免空 prompt 教 LLM "可调用工具" 但其实没工具）
        system_text = SYSTEM_PROMPT
        tool_hint = self._build_tool_usage_hint()
        if tool_hint:
            # 把 tool 使用指引追加到 SYSTEM_PROMPT 末尾
            # 用 "\n\n" 作为段落分隔，跟 SYSTEM_PROMPT 原有结构对齐
            system_text = f"{SYSTEM_PROMPT}\n\n{tool_hint}"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_prompt},
        ]
        # OpenAI 格式的 tools schema：从 registry 里拿，转一次
        tools_schema = self._tools_to_openai_schema()

        # 引用溯源（设计 §6）：累积 agent 查过的 entity_id（去重、按首次出现序）
        cited_entities: list[str] = []

        # 上一轮 LLM 的响应；max_iterations 用完时拿它当 final fallback
        last_response: LLMToolResponse | None = None

        # range(N) 跑 0..N-1，共 N 轮；每轮要么 LLM 给 final，要么调 tool 进下一轮
        for _iteration in range(self.max_iterations):
            response = await self.llm.complete_with_tools(messages=messages, tools=tools_schema)
            last_response = response

            # 没要求调工具 → final answer，解析后返回
            if not response.has_tool_calls():
                raw = response.content or ""
                sections = QASynthesizer._parse_sections(raw)
                return SynthesizedAnswer(sections=sections, raw_output=raw, cited_entities=cited_entities)

            # 有 tool_calls → 把"assistant 这一轮"加进 messages，记录它要调啥
            # OpenAI 协议要求 assistant 的 tool_calls 必须用特定结构回放
            messages.append({
                "role": "assistant",
                "content": response.content,  # 可能是 None
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            # arguments 在 wire 上是 JSON 字符串；我们存 dict 的话要再序列化
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in response.tool_calls
                ],
            })

            # 依次执行每个 tool_call，结果作为 'role=tool' 消息追加
            for tc in response.tool_calls:
                # 收集本次工具调用的 entity_id（引用溯源）
                eid = tc.arguments.get("entity_id")
                if isinstance(eid, str) and eid and eid not in cited_entities:
                    cited_entities.append(eid)
                # 触发"starting"回调（让 SSE 立刻通知前端"LLM 正在调 X"）
                # callback 是可选的；None 时直接跳过判断
                if on_tool_call is not None:
                    # `await` 异步回调；如果回调本身抛错也兜住，不让它打断 ReAct
                    try:
                        await on_tool_call("starting", tc)
                    except Exception:
                        pass

                tool_output = await self._execute_tool_call(tc)

                # 触发"complete"回调（带结果 dict）
                if on_tool_call is not None:
                    try:
                        await on_tool_call("complete", tc, tool_output)
                    except Exception:
                        pass

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    # tool message content 必须是 string；用 json.dumps 把 dict 序列化
                    "content": json.dumps(tool_output, ensure_ascii=False),
                })
            # 进入下一轮 LLM 调用

        # max_iterations 用完仍没给 final answer → 用最后一轮的 content 兜底
        # 让用户至少看到 LLM 最后想说啥；不抛错保持稳健
        raw = (last_response.content if last_response else "") or ""
        if raw:
            sections = QASynthesizer._parse_sections(raw)
        else:
            sections = [{
                "type": "overview",
                "title": "未完成",
                "content": f"ReAct 循环达到 {self.max_iterations} 轮上限仍未收敛，请简化问题或拆分。",
                "references": [],
            }]
        return SynthesizedAnswer(sections=sections, raw_output=raw, cited_entities=cited_entities)

    # ─── 内部 helper ─────────────────────────────────────────────────────

    # ─── v1.7 streaming ───────────────────────────────────────────────────

    # 伪流式 chunk 大小（字符）：太大不像打字机，太小事件过密；20 是 ChatGPT 体感
    _PSEUDO_STREAM_CHUNK_SIZE = 20
    # 每个 chunk 之间的 sleep（秒）：太长拖慢整体，太短没视觉效果
    _PSEUDO_STREAM_INTERVAL_SEC = 0.025

    async def synthesize_stream(
        self,
        ctx: RetrievedContext,
        history: list[dict[str, Any]] | None = None,
        on_token: Optional[Callable[[str], Awaitable[None]]] = None,
        on_thinking: Optional[Callable[[str], Awaitable[None]]] = None,
        on_tool_call: Optional[Callable[..., Awaitable[None]]] = None,
    ) -> SynthesizedAnswer:
        """流式版的 synthesize：跟 synthesize 同样做 ReAct 循环。

        v1.8 起：
          - LLM provider 有 `complete_stream_with_tools` → **真流式**（首 token 即可见）
          - 否则回退 v1.7 伪流式（拿到 complete_with_tools 完整响应后分块）

        :param on_token: 文本片段到达时回调（每个 StreamTextDelta / 每个伪流 chunk）
        :param on_tool_call: tool 执行前后回调，跟 synthesize 一样
        """
        from src.service.qa_engine.prompts import SYSTEM_PROMPT, build_user_prompt
        from src.service.qa_engine.synthesizer import _ctx_to_dict

        user_prompt = build_user_prompt(ctx.question, _ctx_to_dict(ctx))
        system_text = SYSTEM_PROMPT
        tool_hint = self._build_tool_usage_hint()
        if tool_hint:
            system_text = f"{SYSTEM_PROMPT}\n\n{tool_hint}"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_prompt},
        ]
        tools_schema = self._tools_to_openai_schema()
        last_raw_output = ""

        # 引用溯源（设计 §6）：累积 agent 查过的 entity_id（去重、按首次出现序）
        cited_entities: list[str] = []

        # 判定 provider 能不能走真流：duck-typing
        has_real_stream = hasattr(self.llm, "complete_stream_with_tools") and callable(
            getattr(self.llm, "complete_stream_with_tools", None)
        )

        for _iteration in range(self.max_iterations):
            if has_real_stream:
                # ─── 真流式路径（v1.8）─────────────────────────────────
                # 每轮收集本轮的 text + tool_calls
                round_text_buf: list[str] = []
                round_tool_calls: list[ToolCall] = []
                async for event in self.llm.complete_stream_with_tools(
                    messages=messages, tools=tools_schema,
                ):
                    if isinstance(event, StreamTextDelta):
                        round_text_buf.append(event.text)
                        # 立刻推给 on_token（首 token 即可见的关键）
                        if on_token is not None:
                            try:
                                await on_token(event.text)
                            except Exception:
                                pass
                    elif isinstance(event, StreamThinkingDelta):
                        # 思考增量 → on_thinking（设计 §5 灰字）；不进 round_text_buf（不污染答案）
                        if on_thinking is not None:
                            try:
                                await on_thinking(event.text)
                            except Exception:
                                pass
                    elif isinstance(event, ToolCall):
                        round_tool_calls.append(event)
                round_content = "".join(round_text_buf)
                last_raw_output = round_content
            else:
                # ─── 伪流路径（v1.7 兼容）──────────────────────────────
                response = await self.llm.complete_with_tools(messages=messages, tools=tools_schema)
                round_content = response.content or ""
                round_tool_calls = list(response.tool_calls)
                last_raw_output = round_content

            # 没有 tool_calls → 这是最终答案那一轮
            if not round_tool_calls:
                # 伪流路径：还要分块推出（真流路径已经在循环里推过了）
                if not has_real_stream and on_token is not None and round_content:
                    await self._pseudo_stream(round_content, on_token)
                sections = QASynthesizer._parse_sections(round_content)
                return SynthesizedAnswer(sections=sections, raw_output=round_content, cited_entities=cited_entities)

            # 有 tool_calls → 继续 ReAct 循环
            # 1) 把"assistant 这一轮"加进 messages
            messages.append({
                "role": "assistant",
                "content": round_content or None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in round_tool_calls
                ],
            })
            # 2) 依次执行
            for tc in round_tool_calls:
                # 收集本次工具调用的 entity_id（引用溯源）
                eid = tc.arguments.get("entity_id")
                if isinstance(eid, str) and eid and eid not in cited_entities:
                    cited_entities.append(eid)
                if on_tool_call is not None:
                    try:
                        await on_tool_call("starting", tc)
                    except Exception:
                        pass
                tool_output = await self._execute_tool_call(tc)
                if on_tool_call is not None:
                    try:
                        await on_tool_call("complete", tc, tool_output)
                    except Exception:
                        pass
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(tool_output, ensure_ascii=False),
                })

        # max_iterations 用完仍没给 final answer → 兜底
        raw = last_raw_output
        if raw:
            sections = QASynthesizer._parse_sections(raw)
        else:
            sections = [{
                "type": "overview",
                "title": "未完成",
                "content": f"ReAct 循环达到 {self.max_iterations} 轮上限仍未收敛，请简化问题或拆分。",
                "references": [],
            }]
        return SynthesizedAnswer(sections=sections, raw_output=raw, cited_entities=cited_entities)

    @classmethod
    async def _pseudo_stream(
        cls,
        text: str,
        on_token: Callable[[str], Awaitable[None]],
    ) -> None:
        """把完整文本按固定大小分块，每块之间 sleep 一会儿，喂给 on_token。

        on_token 抛错时吞掉（跟 v1.6 行为一致），不打断后续 chunks。
        """
        # range(start, stop, step) 按步长走 0/20/40/...
        for i in range(0, len(text), cls._PSEUDO_STREAM_CHUNK_SIZE):
            chunk = text[i: i + cls._PSEUDO_STREAM_CHUNK_SIZE]
            try:
                await on_token(chunk)
            except Exception:
                pass
            # sleep 让出控制权，让 SSE emitter 主循环有机会 flush 给前端
            await asyncio.sleep(cls._PSEUDO_STREAM_INTERVAL_SEC)

    def _build_tool_usage_hint(self) -> str:
        """根据 ToolRegistry 里的工具列表，生成"可用工具 + 使用规则"的提示词。

        空 registry → 返回 ''（调用方不会拼接到 system prompt 里）
        """
        tools = self.tool_registry.list_tools()
        if not tools:
            return ""

        # 用 join 拼工具列表；每行格式："  - name: description"
        # 比 f-string + for 循环更紧凑
        tool_lines = "\n".join(
            f"  - {t.name}: {t.description}" for t in tools
        )
        # 这段 prompt 着重 3 件事，按照 PetClinic 实测踩过的坑：
        #   1. 告诉 LLM 有哪些工具 → 解决"不知道能调什么"
        #   2. 强调用【可用 context】里给的 entity_id → 解决"瞎编 method://xxx"
        #   3. 强调 project_id 从用户问题或上下文里来 → 解决"猜成 petclinic-root 这种模块名"
        return f"""═════════════════════════════════════════════════════════════
【可调用工具（v1.3 ReAct）】
═════════════════════════════════════════════════════════════

你**可以调用以下工具**做主动查询；每次只在确实需要补充信息时调用，能省则省：

{tool_lines}

工具使用规则（**严格遵守**）：

1. **优先用【可用 context】里已经检索好的 candidates**。我（系统）在你看到的 prompt 里已经做过初步语义检索，
   candidates 里的 entity_id 是**真实存在**的；别自己编 `method://xxx` / `class://xxx`，那样工具会查不到。

2. **entity_id 要照搬**。candidates 里给你的形如 `method//abc123` 或 `class//def456`，
   `://`/`//` 全部保留原样，**不要**把它改成 `method://xxx_imagined` 之类。

3. **project_id 跟用户当前会话保持一致**（默认从用户问题语境推断，比如 petclinic / deposit）；
   不要拿模块 id（如 petclinic-root）当 project_id —— 它们不是一回事。

4. **能给最终答案就别再调工具**。tool_call 仅用于"我看了 candidates 还差关键信息"的场景；
   如果 candidates 已经足够回答，直接输出 6 段式 JSON。

5. **错就早错**。tool 返回 `{{"error": "..."}}` 时不要重复同一调用；改 entity_id 或换工具。
"""

    def _tools_to_openai_schema(self) -> list[dict[str, Any]]:
        """把 ToolRegistry 里的 Tool 翻译成 OpenAI tool calling 的 schema 格式。

        OpenAI 协议：
          [{ "type": "function", "function": {"name": ..., "description": ..., "parameters": <jsonschema>} }, ...]
        """
        # 列表推导式：对 registry 里每个 tool 生成一条 schema
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            }
            for tool in self.tool_registry.list_tools()
        ]

    async def _execute_tool_call(self, tc: ToolCall) -> dict[str, Any]:
        """执行一次 ToolCall，返回 dict（供 messages 序列化）。

        异常一律转成 dict 形式的错误信号，永不抛 —— 一个工具错了不应该让整个 ReAct 失败。
        """
        try:
            return await self.tool_registry.call(tc.name, tc.arguments)
        except ToolNotFound:
            return {"error": f"tool not registered: {tc.name!r}"}
        except Exception as e:
            # 任何 handler 内未捕获的异常都吃掉 —— LLM 看到 error 字段会换工具或自我修复
            return {"error": f"tool execution failed: {e}"}
