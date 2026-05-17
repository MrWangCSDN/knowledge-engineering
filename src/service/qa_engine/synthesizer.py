"""LLM 合成阶段：把检索到的 context 喂给 LLM，输出 6 段式 JSON。

设计文档：[[首页设计]] §6.1（端到端 sequence）

跨仓依赖：实际 LLM provider 来自主仓 src/llm/factory.py。
本模块用 Protocol 抽象，运行时注入。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
# AsyncIterator: complete_stream 的返回值类型
# Awaitable / Callable: on_token 回调类型
from typing import Any, AsyncIterator, Awaitable, Callable, Optional, Protocol

from src.service.qa_engine.prompts import (
    SYSTEM_PROMPT,
    _CHIT_CHAT_SYSTEM,  # v1.2 chit-chat 专属
    build_user_prompt,
    build_user_prompt_with_history,
    build_chitchat_user_prompt,
    with_memory_block,
)
from src.service.qa_engine.retriever import RetrievedContext


# ─── LLM provider 抽象 ─────────────────────────────────────────────────────

class LLMProviderProto(Protocol):
    """跟主仓 LLMProviderFactory 创建的 provider 兼容。"""

    async def complete(self, *, system: str, user: str, **kwargs: Any) -> str:
        """同步式：把 system + user prompt 喂给模型，等完整答复返回。"""
        ...


class StreamingLLMProto(Protocol):
    """v1.6 起的可选流式接口；DashScopeProvider 实现这个。"""

    def complete_stream(
        self, *, system: str, user: str, **kwargs: Any,
    ) -> AsyncIterator[str]:
        """流式：yield 每个文本 chunk；调用方负责累计 + 调 on_token 回调。

        注意签名：返回 AsyncIterator[str]，本身不是 async function（不要写 `async def`）。
        但 yield 的实现是 async generator —— 调用方用 `async for chunk in provider.complete_stream(...)`。
        """
        ...


# ─── 答案数据结构 ──────────────────────────────────────────────────────────

@dataclass
class SynthesizedAnswer:
    """合成后的答案。"""

    sections: list[dict] = field(default_factory=list)
    """6 段式结构化内容（type/title/content/references）。"""

    token_usage: int = 0
    """约束估算（粗算 word count）；后续可从 LLM provider 拿真实值。"""

    cost_yuan: float = 0.0
    """v1 暂未填，留 W8 接 LLM provider 价格表。"""

    raw_output: str = ""
    """原始 LLM 输出（debug/记录用）。"""


# ─── synthesizer ───────────────────────────────────────────────────────────

class QASynthesizer:
    """把 RetrievedContext + LLM 合成为结构化答案。"""

    def __init__(self, llm_provider: LLMProviderProto):
        self.llm = llm_provider

    async def _synthesize_chit_chat(
        self, ctx: RetrievedContext, *, memory_block: str | None = None,
        history: list[dict] | None = None,
    ) -> SynthesizedAnswer:
        """v1.2 chit-chat 闲聊路径：用专属 prompt 调 LLM，返回单段 chit-chat section。
        设计：[[chit-chat-闲聊路径-设计]] §4.3, §4.4；记忆注入见 [[记忆系统-设计]] §7。
        §20：带最近历史原文（旧轮由 memory_block 的 working_summary 覆盖）。"""
        reply = await self.llm.complete(
            system=with_memory_block(_CHIT_CHAT_SYSTEM, memory_block),
            user=build_chitchat_user_prompt(ctx.question, history),
        )
        return SynthesizedAnswer(
            sections=[{
                "type": "chit-chat",
                "title": "",          # 前端不显示 h3 header
                "content": reply,
                "references": [],
            }],
            token_usage=len(reply.split()),  # 粗算 token；后续可从 LLM provider 拿真实值
            cost_yuan=0.0,
            raw_output=reply,
        )

    async def _synthesize_chit_chat_stream(
        self,
        ctx: RetrievedContext,
        on_token: Optional[Callable[[str], Awaitable[None]]] = None,
        *,
        memory_block: str | None = None,
        history: list[dict] | None = None,
    ) -> SynthesizedAnswer:
        """v1.2 chit-chat 流式版：边收 LLM token 边调 on_token。
        设计：[[chit-chat-闲聊路径-设计]] §4.3, §4.6；记忆注入见 [[记忆系统-设计]] §7。
        §20：带最近历史原文（旧轮由 memory_block 的 working_summary 覆盖）。"""
        parts: list[str] = []
        async for tok in self.llm.complete_stream(
            system=with_memory_block(_CHIT_CHAT_SYSTEM, memory_block),
            user=build_chitchat_user_prompt(ctx.question, history),
        ):
            parts.append(tok)
            if on_token is not None:
                await on_token(tok)
        reply = "".join(parts)
        return SynthesizedAnswer(
            sections=[{
                "type": "chit-chat",
                "title": "",
                "content": reply,
                "references": [],
            }],
            token_usage=len(reply.split()),
            cost_yuan=0.0,
            raw_output=reply,
        )

    async def synthesize(
        self,
        ctx: RetrievedContext,
        *,
        history: list[dict] | None = None,
        memory_block: str | None = None,
    ) -> SynthesizedAnswer:
        """主入口（同步式，v1 不流式）。

        Steps:
          1. 把 ctx 转 dict 喂给 prompts.build_user_prompt
          2. 调 LLM 拿 raw 输出
          3. 解析 6 段式 JSON（失败时降级为单段 markdown）
        """
        # v1.2: chit-chat 走专属分支，跳过 6 段式逻辑
        if ctx.skill_id == "chit-chat":
            return await self._synthesize_chit_chat(
                ctx, memory_block=memory_block, history=history
            )

        ctx_dict = _ctx_to_dict(ctx)
        if history:
            user_prompt = build_user_prompt_with_history(
                ctx.question, ctx_dict, history=history
            )
        else:
            user_prompt = build_user_prompt(ctx.question, ctx_dict)

        # 1. 调 LLM
        try:
            raw = await self.llm.complete(
                system=with_memory_block(SYSTEM_PROMPT, memory_block),
                user=user_prompt,
            )
        except Exception as e:
            # LLM 调用本身失败 → 返回错误段（不抛错）
            return SynthesizedAnswer(
                sections=[
                    {
                        "type": "overview",
                        "title": "出错了",
                        "content": f"LLM 调用失败：{e}",
                        "references": [],
                    }
                ],
                raw_output=str(e),
            )

        # 2. 解析 + 兜底
        sections = self._parse_sections(raw)

        # 3. 估算 token usage（粗算，W8 后端再补真实值）
        # 注：memory_block 未计入 token 估算（P1 有意为之；token_usage 本就是粗算，
        # 见 [[记忆系统-设计]] P1）。后续接计费/配额时需显式补上。
        approx_tokens = _estimate_tokens(SYSTEM_PROMPT, user_prompt, raw)

        return SynthesizedAnswer(
            sections=sections,
            token_usage=approx_tokens,
            raw_output=raw,
        )

    # ─── v1.6 streaming ───────────────────────────────────────────────────

    async def synthesize_stream(
        self,
        ctx: RetrievedContext,
        on_token: Optional[Callable[[str], Awaitable[None]]] = None,
        *,
        history: list[dict] | None = None,
        memory_block: str | None = None,
    ) -> SynthesizedAnswer:
        """流式版本的 synthesize：边收 LLM token 边调 on_token 回调。

        要求 `self.llm` 实现 `complete_stream`；没实现就抛 AttributeError（早暴露问题）。

        :param on_token: 每个 chunk 来时调用；签名 `async (chunk: str) -> None`
                         SSE emitter 用这个把 token 转发到前端
        :return: 累计完整后再解析的 SynthesizedAnswer
        """
        # v1.2: chit-chat 走专属流式分支
        if ctx.skill_id == "chit-chat":
            return await self._synthesize_chit_chat_stream(
                ctx, on_token=on_token, memory_block=memory_block, history=history
            )

        ctx_dict = _ctx_to_dict(ctx)
        if history:
            user_prompt = build_user_prompt_with_history(ctx.question, ctx_dict, history=history)
        else:
            user_prompt = build_user_prompt(ctx.question, ctx_dict)

        # 累计 chunks 到这个 buffer，结束后整体解析
        # 用 list + join 比反复 += 字符串高效（Python 字符串不可变）
        buffer: list[str] = []

        # 任何异常都在这里吃掉，转成 error 段
        try:
            # `complete_stream` 返回 async generator；用 async for 迭代
            async for chunk in self.llm.complete_stream(
                system=with_memory_block(SYSTEM_PROMPT, memory_block),
                user=user_prompt,
            ):
                buffer.append(chunk)
                # 触发回调（让 SSE 立即把 token 推给前端）
                if on_token is not None:
                    try:
                        await on_token(chunk)
                    except Exception:
                        # 回调内部出错不应中断 LLM 流；吞掉继续
                        pass
        except Exception as e:
            # LLM 流断了 → 返回错误段
            return SynthesizedAnswer(
                sections=[{
                    "type": "overview",
                    "title": "出错了",
                    "content": f"LLM 流式调用失败：{e}",
                    "references": [],
                }],
                raw_output="".join(buffer),
            )

        raw = "".join(buffer)
        sections = self._parse_sections(raw)
        # 注：memory_block 未计入 token 估算（P1 有意为之；token_usage 本就是粗算，
        # 见 [[记忆系统-设计]] P1）。后续接计费/配额时需显式补上。
        approx_tokens = _estimate_tokens(SYSTEM_PROMPT, user_prompt, raw)
        return SynthesizedAnswer(
            sections=sections,
            token_usage=approx_tokens,
            raw_output=raw,
        )

    @staticmethod
    def _parse_sections(raw: str) -> list[dict]:
        """解析 LLM 输出。

        优先解析 ```json ... ``` fenced block；
        若没 fence 直接当 JSON 试；
        都失败时降级成单段 markdown。
        """
        # 1. 找 ```json fence
        # 注意：LLM 的 sections.content 里允许嵌入 ```mermaid 这种子代码块，
        # 所以**不能**用 `split("```", 1)[0]`（会被首个内部 fence 截断）。
        # 用 rsplit 从最后一个 ``` 反向定位，保证拿到最外层 fence。
        candidate = raw.strip()
        if "```json" in candidate:
            try:
                # 取 ```json 之后的全部内容
                after_open = candidate.split("```json", 1)[1]
                # 从右往左找最后一个 ```（外层 fence 的关闭标记）
                # 没找到（LLM 忘了关 fence）时返回全部 → after_open 自身
                if "```" in after_open:
                    candidate = after_open.rsplit("```", 1)[0].strip()
                else:
                    candidate = after_open.strip()
            except IndexError:
                pass
        elif candidate.startswith("```"):
            try:
                # 同理：rsplit 找最后一个 ``` 关闭
                after_open = candidate.split("```", 1)[1]
                if "```" in after_open:
                    candidate = after_open.rsplit("```", 1)[0].strip()
                else:
                    candidate = after_open.strip()
            except IndexError:
                pass

        # 2. 尝试解析 JSON
        try:
            data = json.loads(candidate)
            sections = data.get("sections", []) if isinstance(data, dict) else []
            # 过滤无效条目（必须有 type 和 content）
            valid = [
                s for s in sections
                if isinstance(s, dict) and "type" in s and "content" in s
            ]
            if valid:
                return valid
        except (json.JSONDecodeError, ValueError, AttributeError):
            pass

        # 3. 降级：包成单段 markdown
        return [
            {
                "type": "overview",
                "title": "回答",
                "content": raw,
                "references": [],
            }
        ]


# ─── 工具 ───────────────────────────────────────────────────────────────────

def _ctx_to_dict(ctx: RetrievedContext) -> dict:
    """RetrievedContext → 给 prompts.build_user_prompt 用的 dict。"""
    return {
        "entry_candidates": ctx.entry_candidates,
        "callees_by_entry": ctx.callees_by_entry,
        "callers_by_entry": ctx.callers_by_entry,
        "table_access_by_entry": ctx.table_access_by_entry,
        # v1.1：把 skill_id 一并送下去，build_user_prompt 据此加视角偏置提示
        # getattr 是为了向后兼容旧 RetrievedContext 实例（万一缺这个字段）
        "skill_id": getattr(ctx, "skill_id", "architecture"),
    }


def _estimate_tokens(system: str, user: str, output: str) -> int:
    """粗算 token usage。

    中英混合估算：1 token ≈ 1.5 字（中文偏多）。
    准确值未来从 LLM provider 拿。
    """
    total_chars = len(system) + len(user) + len(output)
    return max(1, int(total_chars / 1.5))
