"""LLM 合成阶段：把检索到的 context 喂给 LLM，输出 6 段式 JSON。

设计文档：[[首页设计]] §6.1（端到端 sequence）

跨仓依赖：实际 LLM provider 来自主仓 src/llm/factory.py。
本模块用 Protocol 抽象，运行时注入。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from src.service.qa_engine.prompts import (
    SYSTEM_PROMPT,
    build_user_prompt,
    build_user_prompt_with_history,
)
from src.service.qa_engine.retriever import RetrievedContext


# ─── LLM provider 抽象 ─────────────────────────────────────────────────────

class LLMProviderProto(Protocol):
    """跟主仓 LLMProviderFactory 创建的 provider 兼容。"""

    async def complete(self, *, system: str, user: str, **kwargs: Any) -> str:
        """同步式：把 system + user prompt 喂给模型，等完整答复返回。"""
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

    async def synthesize(
        self,
        ctx: RetrievedContext,
        *,
        history: list[dict] | None = None,
    ) -> SynthesizedAnswer:
        """主入口（同步式，v1 不流式）。

        Steps:
          1. 把 ctx 转 dict 喂给 prompts.build_user_prompt
          2. 调 LLM 拿 raw 输出
          3. 解析 6 段式 JSON（失败时降级为单段 markdown）
        """
        ctx_dict = _ctx_to_dict(ctx)
        if history:
            user_prompt = build_user_prompt_with_history(
                ctx.question, ctx_dict, history=history
            )
        else:
            user_prompt = build_user_prompt(ctx.question, ctx_dict)

        # 1. 调 LLM
        try:
            raw = await self.llm.complete(system=SYSTEM_PROMPT, user=user_prompt)
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
        candidate = raw.strip()
        if "```json" in candidate:
            try:
                candidate = candidate.split("```json", 1)[1].split("```", 1)[0].strip()
            except IndexError:
                pass
        elif candidate.startswith("```"):
            try:
                candidate = candidate.split("```", 1)[1].split("```", 1)[0].strip()
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
    }


def _estimate_tokens(system: str, user: str, output: str) -> int:
    """粗算 token usage。

    中英混合估算：1 token ≈ 1.5 字（中文偏多）。
    准确值未来从 LLM provider 拿。
    """
    total_chars = len(system) + len(user) + len(output)
    return max(1, int(total_chars / 1.5))
