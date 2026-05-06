"""LLM prompt 模板。

设计原则（详见 [[首页设计]] §5.2 + §10.决策日志）：

1. 强制结构化输出（6 段式 JSON），LLM 不能自由发挥
2. 引用强约束：方法名/类名必须真实存在于 context，不允许编造
3. 新鲜度透明：在 sources 段标注解读生成时间
4. 中文输出
5. 缺信息的段落直接省略（不要凑字数）

W6 会在这里进一步加入：
  - 业务术语词典（business_terms.yaml 100 条）
  - few-shot gold doc 范例（30 篇）
"""
from __future__ import annotations

import json
from typing import Any


# ─── System prompt（角色 + 规则）────────────────────────────────────────────

SYSTEM_PROMPT = """你是企业代码知识分析师。你的任务是把代码翻译成业务方/新人能读懂的业务文档。

【严格规则】
1. **不允许编造**：所有方法名、类名、表名必须出自我提供的 context，不能从你的知识里"想当然"
2. **结构化输出**：必须按 6 段式 JSON 输出，缺信息的段落直接省略（不要凑字数）
3. **简洁专业**：每段 50-200 字，不啰嗦
4. **引用标记**：提到方法/类/表时，用 `[entity_id|显示文本]` 格式（前端会转链接）
5. **中文输出**

【6 段式结构】
- overview     业务概述（1-2 句这个流程做什么、面向谁）
- entry_point  入口方法（Controller / API entry 类，附 HTTP 路径）
- call_chain   调用步骤列表（5-10 步，每步 1 行业务说明）
- db_ops       数据库操作（INSERT/UPDATE/DELETE 哪些表）
- rules        关键约束/业务规则
- sources      引用的代码实体 + 业务文档

【输出格式】
必须是合法 JSON（用 ```json fenced code block 包裹）：

```json
{
  "sections": [
    {
      "type": "overview",
      "title": "业务概述",
      "content": "...",
      "references": []
    },
    {
      "type": "entry_point",
      "title": "入口方法",
      "content": "[method://com.bank.openAccount|DepositController.openAccount()]\\n  POST /api/account/deposit/open",
      "references": [
        {"entity_id": "method://com.bank.openAccount", "display_text": "DepositController.openAccount()", "kind": "method"}
      ]
    }
  ]
}
```

每个 reference 字段：
  - entity_id:    形如 'method://...' / 'class://...' / 'table://...' / 'doc://...'
  - display_text: 用户友好的显示文本
  - kind:         'method' | 'class' | 'table' | 'doc'

【缺信息处理】
- 如果某段没有可靠信息，直接不输出该段（sections 数组里少一项即可，不要写空内容）
- 如果完全没找到相关代码，只输出一个 overview 段说明"未找到相关业务逻辑"
"""


# ─── User prompt 组装函数 ──────────────────────────────────────────────────

def build_user_prompt(question: str, context: dict[str, Any]) -> str:
    """把 retriever 返回的 context 拼成 LLM user prompt。

    context 结构（来自 RetrievedContext，转 dict）：
      {
        "entry_candidates": [{entity_id, summary_text, level}, ...],
        "callees_by_entry": {entity_id: [callee_id, ...]},
        "callers_by_entry": {entity_id: [caller_id, ...]},
        "table_access_by_entry": {entity_id: [{table_id, operation}, ...]},
      }
    """
    parts: list[str] = []
    parts.append(f"【用户问题】{question}")
    parts.append("")
    parts.append("【可用 context】")

    # 1. 候选入口方法
    candidates = context.get("entry_candidates") or []
    if candidates:
        parts.append("")
        parts.append("候选入口方法（按相关度倒序）:")
        for i, c in enumerate(candidates[:5], 1):
            entity_id = c.get("entity_id", "?")
            level = c.get("level", "method")
            summary = c.get("summary_text") or "(无业务说明)"
            # 截断过长的 summary 控制 token 数
            if len(summary) > 300:
                summary = summary[:300] + "…"
            parts.append(f"  {i}. {entity_id}  [level={level}]")
            parts.append(f"     业务说明: {summary}")
    else:
        parts.append("（向量库未命中任何候选实体）")

    # 2. 调用关系
    callees = context.get("callees_by_entry") or {}
    if any(callees.values()):
        parts.append("")
        parts.append("调用关系（top-3 候选向下展开）:")
        for entry, downs in callees.items():
            if not downs:
                continue
            parts.append(f"  {entry}")
            for d in downs:
                parts.append(f"      → {d}")

    callers = context.get("callers_by_entry") or {}
    if any(callers.values()):
        parts.append("")
        parts.append("被谁调用（caller，了解使用场景）:")
        for entry, ups in callers.items():
            if not ups:
                continue
            parts.append(f"  {entry}")
            for u in ups:
                parts.append(f"      ← {u}")

    # 3. 数据库访问
    table_access = context.get("table_access_by_entry") or {}
    if any(table_access.values()):
        parts.append("")
        parts.append("数据库访问:")
        for entry, tables in table_access.items():
            if not tables:
                continue
            parts.append(f"  {entry}")
            for t in tables:
                op = t.get("operation", "?")
                tid = t.get("table_id", "?")
                parts.append(f"      {op}  {tid}")

    # 4. 任务指令
    parts.append("")
    parts.append("【任务】")
    parts.append("基于以上 context 回答用户问题。")
    parts.append("严格按 6 段式 JSON 输出，缺信息的段落跳过。")
    parts.append("如果 context 不足以回答（比如候选都不相关），")
    parts.append("只输出一个 overview 段说明：未找到相关业务逻辑，可换个说法重试。")

    return "\n".join(parts)


# ─── 多轮对话上下文压缩（v1 暂未启用，保留接口）──────────────────────────

HISTORY_SUMMARIZE_PROMPT = """以下是用户之前的对话历史。请用 1-2 句话概括重点，作为后续对话的上下文：

{history}

概括："""


def build_user_prompt_with_history(
    question: str,
    context: dict[str, Any],
    history: list[dict] | None = None,
) -> str:
    """v1 简版：把历史 N 轮直接拼到 question 前面，不做压缩。

    超 5 轮时（10 条消息）由 router 截断，这里不再考虑长度。
    """
    if not history:
        return build_user_prompt(question, context)

    base = build_user_prompt(question, context)
    history_text = "\n".join(
        f"[{m.get('role', '?')}] {m.get('content', '')[:200]}" for m in history[-10:]
    )
    return f"【对话历史】\n{history_text}\n\n{base}"


# ─── 便利函数（单测/开发期用）──────────────────────────────────────────────

def dump_user_prompt(question: str, context: dict) -> str:
    """方便调试：打印实际发给 LLM 的 user prompt（含 JSON 化的 context 摘要）。"""
    prompt = build_user_prompt(question, context)
    debug_lines = [
        prompt,
        "",
        "─── debug: raw context (truncated) ───",
        json.dumps(context, ensure_ascii=False, indent=2)[:500] + "…",
    ]
    return "\n".join(debug_lines)
