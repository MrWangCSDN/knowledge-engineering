"""LLM 合成阶段：把检索到的 context 喂给 LLM，输出 6 段式 JSON。

设计文档：[[首页设计]] §6.1（端到端 sequence）

跨仓依赖：实际 LLM provider 来自主仓 src/llm/factory.py。
本模块用 Protocol 抽象，运行时注入。
"""
from __future__ import annotations

# json：标准库的严格 JSON 解析；遇到未转义引号/反斜杠直接抛 JSONDecodeError
import json
# dataclass 装饰器 + field 工厂：给数据类生成 __init__ / __repr__ 等
from dataclasses import dataclass, field
# Any：可任意类型；Protocol：结构化子类型（duck typing），用来抽象 LLM provider
from typing import Any, Protocol

# 2026-06-02：json-repair 是宽容 JSON 解析器，遇到未转义/截断也能尽力还原；
# 在标准 json.loads 失败后用它作为兜底（详见 _parse_sections）
from json_repair import repair_json

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

        策略（按尝试顺序）：
          1. 找 ```json ... ``` fence 抽出 JSON 候选串
          2. 标准 json.loads 试一次（最严格）
          3. 失败 → json-repair 兜底（2026-06-02 新增）：自动补全/转义/修复，宽容解析
          4. 仍失败 → 降级成单段 markdown，把整段 raw 当回答展示
          5. 成功抽到 sections 后，对每段 content 做 _fix_gfm_table_cells 后处理
             —— 把表格 cell 内换行转 <br>，避免前端 GFM parser 把多行表格断成多个段
        """
        # 1. 找 ```json fence
        # candidate 是抽出来的待 parse JSON 串；后续 LLM 抽风的输出尽量先归一到这里
        candidate = raw.strip()
        if "```json" in candidate:
            try:
                # split('```json',1) 取 fence 后内容；再 split('```',1)[0] 取到下一个 ``` 之前
                candidate = candidate.split("```json", 1)[1].split("```", 1)[0].strip()
            except IndexError:
                # split 失败说明 fence 结构异常（极少见），保持原 candidate
                pass
        elif candidate.startswith("```"):
            # 没标 lang 的裸 ``` fence — 同样剥一层
            try:
                candidate = candidate.split("```", 1)[1].split("```", 1)[0].strip()
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
