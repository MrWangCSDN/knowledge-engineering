"""LLM 合成阶段：把检索到的 context 喂给 LLM，输出 6 段式 JSON。

设计文档：[[首页设计]] §6.1（端到端 sequence）

跨仓依赖：实际 LLM provider 来自主仓 src/llm/factory.py。
本模块用 Protocol 抽象，运行时注入。
"""
from __future__ import annotations

import json
import re
# 2026-06-02：json-repair 是宽容 JSON 解析器，遇到未转义/截断也能尽力还原；
# 在标准 json.loads 失败后用它作为兜底（详见 _parse_sections）
from json_repair import repair_json
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


# ─── 推理模型 think 段剥离 ──────────────────────────────────────────────────
# 设计：推理模型（MiniMax-M2 / DeepSeek-R1 / 类似）输出格式为
# `<think>...思维链...</think>实际答案`，KE 只消费"实际答案"部分。
# 剥离规则：所有 <think>...</think> 对内的内容（含标签本身）一律删除。
# 兼容：未闭合的 <think>（流式中间状态）也剥到末尾，避免前端看到残段。
_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)
_OPEN_THINK_RE = re.compile(r"<think>.*?$", re.DOTALL | re.IGNORECASE)


def strip_think(text: str | None) -> str:
    """从 LLM 输出中剥掉 `<think>...</think>` 段（推理模型 chain-of-thought）。

    输入 None / 空 → 返回 ""。
    支持：闭合 think 段（最常见），以及流式中断 + 未闭合（开标签到末尾全砍）。
    """
    if not text:
        return ""
    # 先砍闭合 pair（DOTALL + 非贪婪）；多对都剥
    cleaned = _THINK_RE.sub("", text)
    # 兜底剥未闭合的（如流式中断在 think 段中间）
    cleaned = _OPEN_THINK_RE.sub("", cleaned)
    return cleaned.strip()


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

    cited_entities: list[str] = field(default_factory=list)
    """agent 实际查过的 entity_id（去重，按首次出现序）；引用溯源用（设计 §6）。"""


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
        §20：带最近历史原文（旧轮由 memory_block 头部的 session summary 顶替，
        S5 已落实读侧 composer 在 qa_router 5b/5c 段）。"""
        raw = await self.llm.complete(
            system=with_memory_block(_CHIT_CHAT_SYSTEM, memory_block),
            user=build_chitchat_user_prompt(ctx.question, history),
        )
        # 剥推理模型的 <think>...</think> 段（MiniMax-M2 等推理模型必需；
        # 普通模型如 qwen-plus 不会产出 think 标签，strip_think 等价 identity）
        reply = strip_think(raw)
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
        §20：带最近历史原文（旧轮由 memory_block 头部的 session summary 顶替，
        S5 已落实读侧 composer 在 qa_router 5b/5c 段）。"""
        parts: list[str] = []
        # 2026-05-22 重构：think 段剥离移到 provider 层（MiniMaxProvider.complete_stream
        # 自己 yield 已 strip 后的 token），synthesizer 这里恢复 simple loop —— 每个 token
        # 来即 forward，零额外延迟。
        # 修复 bug：之前的 stateful filter "buf 留 7 字防标签碎片" 对 DashScope（qwen-plus）
        # 等不输出 <think> 的模型多余，每个 SSE chunk 都被 hold → 前端看着"一片一片吐"
        # 而非逐字打字机。MiniMax 自己处理 think filter，普通模型零开销。
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

        策略（按尝试顺序）：
          1. 找 ```json ... ``` fence 抽出 JSON 候选串（rsplit 处理嵌入 ```mermaid 子 fence）
          2. 标准 json.loads 试一次（最严格）
          3. 失败 → json-repair 兜底（2026-06-02 新增）：自动补全/转义/修复，宽容解析
          4. 仍失败 → 降级成单段 markdown，把整段 raw 当回答展示
          5. 成功抽到 sections 后，对每段 content 做 _fix_gfm_table_cells 后处理
             —— 把表格 cell 内换行转 <br>，避免前端 GFM parser 把多行表格断成多个段
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

        # 2. 第一道：严格 json.loads
        data: Any = None
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            # 2026-06-02：第二道 — json-repair 兜底
            # LLM 在 GFM 表格 / 代码块里偶发会吐 未转义 " 或 \，导致标准 json 直接炸
            # repair_json 会自动补 } / 转义引号 / 修复尾逗号等；
            # return_objects=True 让它返回 Python 对象而不是字符串
            try:
                data = repair_json(candidate, return_objects=True)
            except Exception:
                # json-repair 也救不回来 → data 保持 None，落到下面的兜底
                data = None

        # 3. 抽 sections
        # data 可能是 dict / list / None / 其它 — 只接受 dict 且含 "sections" key
        if isinstance(data, dict):
            sections = data.get("sections", [])
            if isinstance(sections, list):
                # 过滤无效条目（必须含 type 和 content 字段，且 content 是字符串）
                valid = [
                    s for s in sections
                    if isinstance(s, dict)
                    and "type" in s
                    and "content" in s
                    and isinstance(s["content"], str)
                ]
                if valid:
                    # 4. 对每段 content 做 GFM 表格 cell 多行修复
                    # 用浅拷贝 dict 避免改到 caller 持有的引用（防御性）
                    return [
                        {**s, "content": _fix_gfm_table_cells(s["content"])}
                        for s in valid
                    ]

        # 5. 兜底：包成单段 markdown
        # 整段 raw 输出（含 fence + JSON 原文）作为回答展示给用户
        # 也对兜底内容做一次表格修复（不会有损失）
        return [
            {
                "type": "overview",
                "title": "回答",
                "content": _fix_gfm_table_cells(raw),
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


def _fix_gfm_table_cells(content: str) -> str:
    """修复 GFM 表格里 cell 内的换行。

    问题背景：
        GFM 规范要求表格 cell 内容必须单行；多行需用 <br>。
        但 LLM 经常吐这种输出 ——
            | 字段 | 含义        |
            | --- | ---         |
            | name | 用户名
            （必填） |
        中间那行 cell 内换行后，前端 GFM parser 看到 "用户名" 那行不以 | 结尾
        → 整张表渲染断裂（变成 2 行表 + 一段普通文字 + 1 行表）。

    算法：
        - 按行扫描；遇到 `|` 开头的行进入"表格上下文"
        - 表格上下文里若出现"非 | 开头但非空白"的行，且**上一行未以 | 闭合**
          → 判定为 cell 续行，用 <br> 接到上一行尾部
        - 空行 / 上一行已闭合时退出表格上下文

    边界处理：
        - 表头分隔行 (| --- | --- |) 以 | 结尾 → 不会触发合并
        - 表格后正文段：上一行已经以 | 结尾 → 不会被吸进表格
        - 不嵌入复杂的 fence/blockquote 上下文识别 —— 99% 场景这个简单算法够用

    Args:
        content: 单段 markdown 字符串（一般是 section.content）

    Returns:
        修复后的 markdown 字符串；如果没有表格上下文则原样返回
    """
    # 没有 | 直接 short-circuit（绝大多数 section 无表格，省 split 开销）
    if "|" not in content:
        return content

    # split 不带参数会按 \n 切；保留行内不含 \n
    lines = content.split("\n")
    # result 累积修复后的行；in_table 跟踪是否在表格上下文里
    result: list[str] = []
    in_table = False

    for line in lines:
        # 表格行的明确信号：以 `|` 开头
        if line.startswith("|"):
            in_table = True
            result.append(line)
            continue

        # 空白行 → 结束当前表格上下文（GFM 表格被空行打断）
        if not line.strip():
            in_table = False
            result.append(line)
            continue

        # 处于表格上下文 + 上一行未以 `|` 闭合 → 判定为 cell 续行
        # result[-1].rstrip() 去掉尾空白再看是否以 `|` 结尾
        if in_table and result and not result[-1].rstrip().endswith("|"):
            # 合并：上一行末（去掉尾空白）+ <br> + 当前行（去掉首尾空白）
            result[-1] = result[-1].rstrip() + "<br>" + line.strip()
            continue

        # 处于表格上下文但上一行已闭合 → 真的是表格后正文，结束上下文
        in_table = False
        result.append(line)

    return "\n".join(result)
