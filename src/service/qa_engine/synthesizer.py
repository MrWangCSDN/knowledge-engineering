"""LLM 合成阶段：把检索到的 context 喂给 LLM，输出 6 段式 JSON。

设计文档：[[首页设计]] §6.1（端到端 sequence）

跨仓依赖：实际 LLM provider 来自主仓 src/llm/factory.py。
本模块用 Protocol 抽象，运行时注入。
"""
from __future__ import annotations

import json
# 2026-06-02 ：logging 用 stdlib，记录 call_chain JSON 校验/修复诊断
import logging
# 2026-06-02：os 用来读 KE_CALLCHAIN_AUTO_REPAIR 环境变量（feature flag）
import os
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
from src.knowledge.recall_rerank import is_callchain_noise

# 模块级 logger；输出 call_chain 修复成功/失败的诊断信息
log = logging.getLogger(__name__)


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

        # 2.5（2026-06-02）：call_chain JSON schema 校验 + 1 次 LLM retry 自修
        # feature flag KE_CALLCHAIN_AUTO_REPAIR（默认 on）；失败时保留原 content 走前端兜底
        sections = await self._repair_call_chain_sections(sections)
        # 逻辑图中文化（[[逻辑图中文化-设计]] §4.3）：A1 接地校验——LLM 产的 call_chain 节点
        # 必须锚定召回到的真实方法；虚构节点丢弃、有效<2 判废删段（交下方 _ensure 兜底）。
        sections = _ground_call_chain_sections(sections, _recalled_ids(ctx))
        # Fix-2：LLM 没产出 call_chain 但召回到调用链 → 用 ctx 的多跳边确定性注入一段（必出 ReactFlow）
        sections = _ensure_call_chain_section(sections, ctx)

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
        # 2.5（2026-06-02）：流式路径同样在解析完后做 call_chain JSON 校验 + LLM retry
        # 流式 emit 已经把 raw_stream token 推给前端；retry 在 done 之前发生，
        # 前端拿到 final sections 后会自动用 hasSections 分支覆盖 raw_stream 渲染
        sections = await self._repair_call_chain_sections(sections)
        # 逻辑图中文化 §4.3：A1 接地校验（同非流式路径），删幻觉节点/判废段
        sections = _ground_call_chain_sections(sections, _recalled_ids(ctx))
        # Fix-2：同非流式路径——无 call_chain 段则用 ctx 多跳边确定性注入
        sections = _ensure_call_chain_section(sections, ctx)
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

    async def _repair_call_chain_sections(self, sections: list[dict]) -> list[dict]:
        """对 sections 里的 call_chain 段做 schema validate + 1 次 LLM retry 自修。

        策略：
          - 合法 → 不动
          - 不合法 → 把坏 JSON + 错误清单送回 LLM 让它仅修 call_chain.content
          - retry 仍失败 → 保留原 content（前端 fallback 走 mermaid / markdown 显示原文）

        最多 retry 1 次（不递归 / 不无限重试，避免 token 爆炸 + 失败循环）。

        feature flag：
          KE_CALLCHAIN_AUTO_REPAIR=false / 0 / no / off 关闭整个 repair 流程
          默认 on —— 因为它是兜底，不开启 LLM 偶发输出错的 JSON 会让前端图渲染失败

        Args:
            sections: _parse_sections 出来的段列表

        Returns:
            和入参等长的段列表；call_chain 段可能被修复，其它段原样
        """
        # 环境变量关掉 repair → 直接 passthrough
        # .lower() 兼容大小写；set 形式列出公认的"关"值
        flag = os.getenv("KE_CALLCHAIN_AUTO_REPAIR", "true").lower()
        if flag in {"false", "0", "no", "off"}:
            return sections

        # out 累积处理后的段；浅拷贝原段防御性避免 caller 持有的引用被改
        out: list[dict] = []
        for s in sections:
            # 非 call_chain 段不做 JSON 校验，直接收
            if s.get("type") != "call_chain":
                out.append(s)
                continue

            content = s.get("content", "")
            # 第一次校验
            data, errs = _validate_call_chain_content(content)
            if not errs:
                # 合法 JSON + schema 通过 → 不动
                out.append(s)
                continue

            # 校验失败 → 触发 LLM retry 一次
            # log 截前 3 条错误足够诊断；不全 log 防止 prod 日志爆量
            log.info(
                "call_chain JSON 校验失败，触发 LLM 自修；错误样例（前 3 条）: %s",
                errs[:3],
            )
            try:
                # 构造 repair prompt —— broken JSON + 错误清单 + schema 提醒
                repair_user_prompt = _build_call_chain_repair_prompt(content, errs)
                # 用极简 system prompt（节省 token；不重发完整 SYSTEM_PROMPT 几千 token）
                fixed = await self.llm.complete(
                    system=(
                        "你是 JSON 修复助手。严格按用户提供的 schema 修复 JSON，"
                        "**仅输出修复后的 JSON 字面量字符串**，不要任何解释、注释、前后缀文字、或 markdown fence。"
                    ),
                    user=repair_user_prompt,
                )
                # 二次校验 retry 结果
                fixed_data, fixed_errs = _validate_call_chain_content(fixed)
                if not fixed_errs and fixed_data is not None:
                    # 修复成功 → 用规范化 JSON（json.dumps）替换原 content
                    # ensure_ascii=False 保留中文 label 不被转 \uXXXX；
                    # separators=(",",":") 紧凑输出节省 SSE 字节数
                    canonical = json.dumps(
                        fixed_data, ensure_ascii=False, separators=(",", ":")
                    )
                    log.info(
                        "call_chain JSON 修复成功（原 %d chars → 修复后 %d chars）",
                        len(content),
                        len(canonical),
                    )
                    out.append({**s, "content": canonical})
                    continue
                # retry 输出还是不合法 → 留原 content，让前端走 fallback
                log.warning(
                    "call_chain JSON 修复 retry 输出仍不合法；错误样例: %s",
                    fixed_errs[:3] if fixed_errs else ["unknown"],
                )
            except Exception as e:
                # LLM 调用本身炸（超时 / API 错）→ 不抛错，保留原 content
                log.warning("call_chain JSON 修复 LLM 调用异常: %s", e)

            # 修复失败 → 留原 content；前端 tryParseCallChain 失败后走 mermaid / markdown 兜底
            out.append(s)

        return out


# ─── 工具 ───────────────────────────────────────────────────────────────────

# ─── Fix-2：确定性 call_chain 注入（[[召回链路缺陷诊断与修复方案]]）──────────────
# 已召回到调用链（ctx.call_edges_by_entry，C2 产出的多跳保边）但 LLM 没产出 call_chain 段时，
# 后端用这些边确定性构造一段，保证流程类问题出 ReactFlow——不赌 LLM 是否"愿意"画。

# 注入图的节点上限（控图大小 + payload 体积）
_CALLCHAIN_MAX_NODES = 18


def _cc_head(entity_id: str) -> str:
    """去掉 '#(params)' 取 'Class::method' 部分。"""
    # split('#', 1)[0]：'#' 前是 qualified_name 主体
    return (entity_id or "").split("#", 1)[0]


def _cc_label(entity_id: str) -> str:
    """实体 id → 短方法名（去类名/参数），作节点展示 label。"""
    head = _cc_head(entity_id)
    # split('::')[-1]：取最后一段方法名；'or head' 兜底空串
    return head.split("::")[-1] or head


def _cc_class_of(entity_id: str) -> str:
    """实体 id → 类全名（'Class::method' 的 Class 部分）。"""
    head = _cc_head(entity_id)
    # rsplit('::', 1)[0]：从右切一刀取类名部分；无 '::' 返回 ''
    return head.rsplit("::", 1)[0] if "::" in head else ""


def _cc_is_noise(entity_id: str) -> bool:
    """是否调用图噪声（getter/setter、MyBatis Example/CRUD、结果包装类）。

    复用召回降噪的 is_callchain_noise（[[召回降噪加权-设计]]），与 retriever._bfs_edges
    同一口径——BFS 已过滤一道，这里作为注入侧的二次防线（也兜 LLM 段未走 BFS 的情况）。
    """
    return is_callchain_noise(entity_id)


def _cc_kind(entity_id: str) -> str:
    """按类名后缀推断节点角色（前端 MethodNode 据此着色 + 图标，区分调用分层）。

    Controller→controller(🌐蓝)、ServiceImpl/Service→service(⚙️绿)、
    Mapper/Dao→mapper(💾琥珀)、其余→method(⚡灰)。
    """
    cls = _cc_class_of(entity_id).rsplit(".", 1)[-1]  # 短类名（去包名）
    if cls.endswith("Controller"):
        return "controller"
    # ServiceImpl 也以 "Service" 收尾前先判 Impl，二者都归 service 层（同绿色）
    if cls.endswith("ServiceImpl") or cls.endswith("Service"):
        return "service"
    if cls.endswith("Mapper") or cls.endswith("Dao"):
        return "mapper"
    return "method"


# 业务标签断句的分隔符（句读 + 中英空格 + 换行 + 左括号）；2b 解读多为空格分隔的短语列表
_LABEL_SEPS = frozenset("。，；、 　\n（(")


def _short_cn_label(text: str, max_len: int = 16) -> str:
    """从 2b 中文解读提炼一个短 label 作业务流程节点名（取首个完整短语，不切词中间）。

    步骤：① 去掉开头 [摘要]/【…】小节标记；② 在最早的分隔点断句取首短语；首短语太短（<4 字，
    如「获取」）则延到下一个分隔点（→「获取 验证码」）；③ 兜底按 max_len 截断。
    """
    if not text:
        return ""
    text = text.strip()
    # ① 去掉开头短方括号小节标记（[摘要]/【…】；仅短前缀，避免误伤正文方括号）
    if text[:1] in "[【":
        for close in ("]", "】"):
            end = text.find(close)
            if 0 < end < 8:
                text = text[end + 1:].strip()
                break
    # ② 收集所有分隔点位置；在最早处断句，首短语过短则延到下一个分隔点
    cuts = [i for i, ch in enumerate(text) if ch in _LABEL_SEPS and i > 0]
    if cuts:
        cut = cuts[0]
        if cut < 4 and len(cuts) > 1:   # 首短语太短（如「获取」）→ 并上下一段
            cut = cuts[1]
        text = text[:cut]
    # ③ 兜底截断（无分隔点的超长串）
    return text.strip()[:max_len]


def _build_call_chain_section_from_edges(
    call_edges_by_entry: dict | None,
    max_nodes: int = _CALLCHAIN_MAX_NODES,
    node_summaries: dict | None = None,
) -> dict | None:
    """用 ctx.call_edges_by_entry 的多跳边确定性构造一个 call_chain 段。

    Args:
        call_edges_by_entry: {entry_id: [(from_id, to_id), ...]}（C2 产出）
        max_nodes: 节点上限（截断控图大小）
    Returns:
        {"type":"call_chain","title":"调用链路","content":<CallChain JSON 字符串>}；
        无可用边（空 / 全是框架噪声 / 截断后无边）→ None（不注入空图）。
    """
    if not call_edges_by_entry:
        return None

    node_order: list[str] = []                 # 节点首次出现顺序（截断时保前面的）
    node_set: set[str] = set()
    edges_out: list[dict] = []
    seen_edges: set[tuple[str, str]] = set()    # 边去重
    # 节点去重：同一方法的带参/无参 id 形态（_cc_head 去参后相同）合并为一个代表节点，
    # 消除"register 出现两次（一中一英）"。首见形态作代表（其 entityId 可点击、label 命中解读）。
    canon: dict[str, str] = {}

    def _rep(nid: str) -> str:
        key = _cc_head(nid)            # Class::method（去 #参数）
        if key not in canon:
            canon[key] = nid          # 首见形态作代表
        return canon[key]

    # 汇总所有 entry 的边：端点规范化（带参/无参合并）+ 去重 + 过滤框架噪声
    for edges in call_edges_by_entry.values():
        for frm, to in edges:
            if _cc_is_noise(frm) or _cc_is_noise(to):
                continue
            rf, rt = _rep(frm), _rep(to)
            if rf == rt:               # 带参/无参指向同方法 → 自环，跳过
                continue
            key = (rf, rt)
            if key in seen_edges:
                continue
            seen_edges.add(key)
            edges_out.append({"from": rf, "to": rt})
            for nid in (rf, rt):
                if nid not in node_set:
                    node_set.add(nid)
                    node_order.append(nid)

    if not edges_out:
        return None

    # 截断节点控图大小：保留前 max_nodes 个；只留两端都在保留集内的边（避免悬挂边）
    keep = set(node_order[:max_nodes])
    kept_edges = [e for e in edges_out if e["from"] in keep and e["to"] in keep]
    if not kept_edges:
        return None
    # 节点：id=实体 id（与 edges from/to 一致）；label=短方法名；classOf=类全名（前端 hover 显示）；
    # kind=按类名后缀推断的分层角色（前端据此着色+图标）；entityId=method:// scheme（前端 EntityRef
    # 据此点击跳源码、复用代码片段抽屉；后端 resolve_first 解析时会剥掉 scheme）。
    # label：有 2b 中文解读 → 取首句中文业务动作；否则回退方法短名
    # （belt-and-suspenders：确定性兜底图也尽量中文，覆盖到的方法即使 LLM 不产 A1 图也是中文）
    summaries = node_summaries or {}
    nodes = []
    for nid in node_order:
        if nid not in keep:
            continue
        cn = _short_cn_label(summaries.get(nid, ""))
        # 中英结合（治"节点空洞"）：label=中文业务名（无解读则方法短名兜底）；method=英文 class.method
        # （去包名短类名 + 方法），前端两行展示——上行中文业务动作、下行真实代码标识。
        cls_short = _cc_class_of(nid).rsplit(".", 1)[-1]
        method_en = f"{cls_short}.{_cc_label(nid)}" if cls_short else _cc_label(nid)
        nodes.append({
            "id": nid,
            "label": cn or _cc_label(nid),
            "method": method_en,
            "classOf": _cc_class_of(nid),
            "kind": _cc_kind(nid),
            "entityId": f"method://{nid}",
        })

    # content 为合法 CallChain JSON 字符串（不包 ```json fence），与前端 tryParseCallChain 对齐
    content = json.dumps({"nodes": nodes, "edges": kept_edges}, ensure_ascii=False)
    return {"type": "call_chain", "title": "调用链路", "content": content}


def _recalled_ids(ctx) -> set[str]:
    """从 ctx.call_edges_by_entry 汇总调用链上全部真实方法 id（边的去重端点集）。

    用作 A1 接地校验的「合法锚点全集」——真实方法即使无 2b 解读也算合法锚点。
    """
    ids: set[str] = set()
    # getattr 兼容：ctx 可能是旧实例 / 测试桩
    for edges in getattr(ctx, "call_edges_by_entry", {}).values():
        for frm, to in edges:
            ids.add(frm)
            ids.add(to)
    return ids


def _ground_call_chain_sections(sections: list[dict], recalled_method_ids: set[str]) -> list[dict]:
    """A1 接地校验（[[逻辑图中文化-设计]] §4.3）：LLM 产的 call_chain 节点必须锚定真实方法。

    规则：
      - 节点 entityId（剥 method:// scheme）∈ recalled_method_ids → 保留；不在（虚构）→ 丢节点；
      - 丢节点后，引用被丢节点的边一并删（避免悬挂边）；
      - 有效节点 < 2 → 该 call_chain 段判废、整段删除（交 _ensure 兜底重注入方法图）；
      - content 非法 JSON → 同样判废删段；
      - 边只校验 from/to 都引用保留下来的 node id（**允许逻辑边，不要求是真实 call**——抽象本质）。
    非 call_chain 段原样返回。

    两个守卫（实现期发现）：
      1) recalled_method_ids 为空（无调用边）→ 无从接地，整体原样返回——此时 call_chain 可能
         来自 mermaid / LLM 自有知识，不应被误删；
      2) content 非 JSON（如 ```mermaid fence）→ 接地不适用，原样保留该段（不删）。
    """
    # 守卫 1：没有可接地的真实方法集 → 原样返回（不动任何 call_chain）
    if not recalled_method_ids:
        return sections
    out: list[dict] = []
    for sec in sections:
        # 非 call_chain 段：原样保留
        if sec.get("type") != "call_chain":
            out.append(sec)
            continue
        # 解析 content（CallChain JSON 字符串）
        try:
            data = json.loads(sec.get("content") or "")
            nodes = data.get("nodes") or []
            edges = data.get("edges") or []
        except (ValueError, TypeError):
            # 守卫 2：非 JSON CallChain（mermaid 等）→ 接地不适用，原样保留（不误删合法图）
            out.append(sec)
            continue
        # 逐节点接地：entityId 剥 scheme（split('://',1)[-1] 对裸 qn 无副作用）后判是否真实
        kept_nodes = []
        kept_node_ids: set[str] = set()
        for n in nodes:
            anchor = (n.get("entityId") or "").split("://", 1)[-1]
            if anchor in recalled_method_ids:
                kept_nodes.append(n)
                kept_node_ids.add(n.get("id"))
        # 有效节点 < 2 → 判废删段
        if len(kept_nodes) < 2:
            continue
        # 边：from/to 都在保留节点里才留（允许逻辑边，只防悬挂）
        kept_edges = [
            e for e in edges
            if e.get("from") in kept_node_ids and e.get("to") in kept_node_ids
        ]
        # 重写 content（保留 title 等其它字段）
        new_sec = dict(sec)
        new_sec["content"] = json.dumps(
            {"nodes": kept_nodes, "edges": kept_edges}, ensure_ascii=False
        )
        out.append(new_sec)
    return out


def _ensure_call_chain_section(sections: list[dict], ctx) -> list[dict]:
    """Fix-2：sections 无 call_chain 段、但 ctx 有多跳调用边 → 确定性注入一段。

    插入位置：entry_point 段之后（自然位置）> overview 之后 > 末尾。
    已有 call_chain 段（LLM 自己产出）→ 原样返回，不重复注入。
    """
    if any(s.get("type") == "call_chain" for s in sections):
        return sections
    built = _build_call_chain_section_from_edges(
        getattr(ctx, "call_edges_by_entry", None),
        node_summaries=getattr(ctx, "callchain_node_summaries", None),
    )
    if built is None:
        return sections
    # 找插入锚点：entry_point 之后 > overview 之后 > append
    idx = next((i for i, s in enumerate(sections) if s.get("type") == "entry_point"), None)
    if idx is None:
        idx = next((i for i, s in enumerate(sections) if s.get("type") == "overview"), None)
    if idx is None:
        sections.append(built)
    else:
        sections.insert(idx + 1, built)
    return sections


def _ctx_to_dict(ctx: RetrievedContext) -> dict:
    """RetrievedContext → 给 prompts.build_user_prompt 用的 dict。"""
    return {
        "entry_candidates": ctx.entry_candidates,
        "callees_by_entry": ctx.callees_by_entry,
        # C2/Fix-2：多跳调用边——build_user_prompt 的「调用链路」块 + 确定性注入都需要它。
        # 之前漏带（C2 gap）→ prompt 调用链块在生产里一直空 → LLM 看不到多跳边。getattr 兼容旧实例。
        "call_edges_by_entry": getattr(ctx, "call_edges_by_entry", {}),
        # 逻辑图中文化（[[逻辑图中文化-设计]] §4.2）：调用链方法的 2b 中文解读，喂 LLM 写业务标签
        "callchain_node_summaries": getattr(ctx, "callchain_node_summaries", {}),
        # source-first grounding P1（[[业务问答-源码优先接地-P1设计]]）：候选真实源码片段，
        # build_user_prompt 渲染给 LLM 作代码事实依据（治代码细节臆造）。getattr 兼容旧实例。
        "candidate_code_snippets": getattr(ctx, "candidate_code_snippets", {}),
        "callers_by_entry": ctx.callers_by_entry,
        "table_access_by_entry": ctx.table_access_by_entry,
        # v1.1：把 skill_id 一并送下去，build_user_prompt 据此加视角偏置提示
        # getattr 是为了向后兼容旧 RetrievedContext 实例（万一缺这个字段）
        "skill_id": getattr(ctx, "skill_id", "architecture"),
        # 候选树（[[候选按调用顺序组装-设计]]）：build_user_prompt 据此选 tree / flat 分支
        # 旧 ctx 没这字段 → getattr 兜底 None → prompt 走原扁平（向后兼容）
        "candidate_tree": getattr(ctx, "candidate_tree", None),
    }


def _estimate_tokens(system: str, user: str, output: str) -> int:
    """粗算 token usage。

    中英混合估算：1 token ≈ 1.5 字（中文偏多）。
    准确值未来从 LLM provider 拿。
    """
    total_chars = len(system) + len(user) + len(output)
    return max(1, int(total_chars / 1.5))


# ─── call_chain JSON schema 校验 + LLM 自修（2026-06-02 新增）─────────────────

# 节点 id 必须是 ASCII 标识符：以字母开头 + 字母/数字/下划线
# 这条约束跟前端 ReactFlow / dagre / mermaid 一致；防止 LLM 吐含 . / / 空格 / 中文的 id
_VALID_NODE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

# 节点 kind 允许的枚举（用于配色 + 前端 MethodNode 图标）
# 跟前端 src/types/chat.ts CallChainNode.kind 严格对齐
_ALLOWED_KINDS = frozenset({"controller", "service", "mapper", "method", "external"})


def _validate_call_chain_content(content: str) -> tuple[dict | None, list[str]]:
    """校验 call_chain.content 是否合法 CallChainData JSON。

    校验项（按顺序短路）：
      1. content 是合法 JSON（含 json-repair 兜底）
      2. 顶层是 dict（不能是 array / number / null）
      3. nodes / edges 字段是 list
      4. 每个 node 是 dict + 有 id（ASCII 标识符）+ 有 label
      5. 每个 node 的 kind（如有）必须在 _ALLOWED_KINDS 里
      6. 每条 edge 有 from/to + 它们都在 nodes.id 集合里（无悬挂边）

    Returns:
        合法时：(parsed_data_dict, [])
        不合法时：(None, errors_list)
        —— errors 是给 LLM 看的中文人类可读错误列表
    """
    # 边界：空 content 直接返回 错误
    if not content or not content.strip():
        return None, ["content 为空"]

    # 1. 防御性剥 markdown fence —— LLM 偶尔会把 JSON 包成 ```json fenced 即使我们要求不要包
    candidate = content.strip()
    if candidate.startswith("```"):
        # 找第一个换行后到最后一个 ``` 之间的内容
        first_nl = candidate.find("\n")
        last_fence = candidate.rfind("```")
        if first_nl > 0 and last_fence > first_nl:
            candidate = candidate[first_nl + 1 : last_fence].strip()

    # 2. 尝试 parse：json.loads → json-repair fallback
    data: Any = None
    try:
        data = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        try:
            # json-repair 是宽容解析器，补缺 } / 转义 / 修尾逗号
            data = repair_json(candidate, return_objects=True)
        except Exception:
            return None, ["content 不是合法 JSON（json-repair 也无法解析）"]

    # 3. 顶层必须是 dict
    if not isinstance(data, dict):
        return None, [f"JSON 顶层必须是 object，实际是 {type(data).__name__}"]

    errors: list[str] = []
    nodes_raw = data.get("nodes")
    edges_raw = data.get("edges")

    # 4. nodes / edges 必须是 list
    if not isinstance(nodes_raw, list):
        return None, ["'nodes' 字段缺失或不是数组"]
    if not isinstance(edges_raw, list):
        return None, ["'edges' 字段缺失或不是数组"]

    # 5. 逐节点校验
    node_ids: set[str] = set()
    for i, n in enumerate(nodes_raw):
        if not isinstance(n, dict):
            errors.append(f"nodes[{i}] 必须是 object")
            continue
        nid = n.get("id")
        if not isinstance(nid, str) or not nid:
            errors.append(f"nodes[{i}].id 缺失或不是非空字符串")
        elif not _VALID_NODE_ID_RE.match(nid):
            errors.append(
                f"nodes[{i}].id '{nid}' 必须以字母开头，仅含 ASCII [A-Za-z0-9_]"
            )
        else:
            # 只把合法 id 加入集合 —— 不合法 id 直接当作"不存在"，让边校验顺便发现
            node_ids.add(nid)
        label = n.get("label")
        if not isinstance(label, str) or not label.strip():
            errors.append(f"nodes[{i}].label 缺失或为空")
        # kind 字段可选；填了就必须在枚举里
        kind = n.get("kind")
        if kind is not None and kind not in _ALLOWED_KINDS:
            errors.append(
                f"nodes[{i}].kind '{kind}' 不在允许枚举 {sorted(_ALLOWED_KINDS)}"
            )

    # 6. 逐边校验
    for i, e in enumerate(edges_raw):
        if not isinstance(e, dict):
            errors.append(f"edges[{i}] 必须是 object")
            continue
        for end in ("from", "to"):
            ref = e.get(end)
            if not isinstance(ref, str) or not ref:
                errors.append(f"edges[{i}].{end} 缺失或不是非空字符串")
            elif ref not in node_ids:
                errors.append(
                    f"edges[{i}].{end} '{ref}' 引用了 nodes 里不存在的 id（悬挂边）"
                )

    if errors:
        return None, errors
    return data, []


def _build_call_chain_repair_prompt(broken_content: str, errors: list[str]) -> str:
    """构造发给 LLM 的 repair prompt：broken JSON + 错误清单 + schema 提醒。

    设计要点：
      - 错误列表截前 20 条避免 token 爆（实际不会这么多）
      - schema 给详细字段注释（让 LLM 知道哪些必填 / 可选）
      - 强调"只输出修复后的 JSON 字面量"——防止 LLM 又包 markdown / 加解释
      - 提供具体修复策略（id 含非法字符怎么办、悬挂边怎么办、kind 非法怎么办）
    """
    err_list = "\n".join(f"- {e}" for e in errors[:20])
    if len(errors) > 20:
        err_list += f"\n（…还有 {len(errors) - 20} 条错误未列出）"

    # 注意：内部花括号 {{ }} 转义，防止被 f-string 当占位符
    return f"""你之前输出的 call_chain JSON 没通过 schema 校验。请修复后**仅返回修复后的 JSON 字面量字符串**——不要包外层 ```json fence，不要任何解释/注释/前后缀文字。

【原始 JSON】
{broken_content}

【错误清单】
{err_list}

【目标 schema】
{{
  "nodes": [
    {{
      "id": "n1",                              // 必填，ASCII 标识符：字母开头 + 只含 [A-Za-z0-9_]
      "label": "OrderController.create",       // 必填，节点显示文本
      "kind": "controller",                    // 可选；枚举 controller/service/mapper/method/external
      "classOf": "com.foo.OrderController",    // 可选
      "sig": "(OrderParam)",                   // 可选
      "filePath": "src/.../OrderController.java",  // 可选
      "lineNumber": 45,                        // 可选
      "entityId": "method://..."               // 可选
    }}
  ],
  "edges": [
    {{"from": "n1", "to": "n2", "label": "调用业务层"}}  // from/to 必填，必须引用 nodes 里存在的 id
  ]
}}

【修复规则】
- id 含 . / / 空格 / 中文 / 标点 → 用下划线替换（如 'com.foo.Bar' → 'com_foo_Bar'）
- 边的 from/to 引用了不存在的 id → 把那条边删除（不要凭空加节点）
- kind 不在枚举里 → 用 'method'
- 节点 label 缺失或为空 → 用 id 当 label
- 必要时调整 nodes/edges 顺序让 from/to 引用合法

输出**只有**修复后的 JSON 字面量字符串："""


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
