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

═════════════════════════════════════════════════════════════
【Step 1：先选「视角」】
═════════════════════════════════════════════════════════════

根据用户问题判断采用哪种视角作答。视角不同 → 答案侧重点不同。
在 overview 段开头注明"视角：xxx"（一行即可），然后按该视角侧重组织其他段。

| 视角 ID | 适合场景 | 答案侧重 |
| --- | --- | --- |
| `overall-architecture` | "怎么实现的"、"是什么"、模糊宽泛问题 | entry_point + call_chain，画整体架构图 |
| `request-lifecycle` | "API X 的处理流程"、"请求怎么走" | entry_point + call_chain（按时序展开）|
| `data-flow` | "数据怎么流的"、"哪里写表"、"用了什么数据" | db_ops 重点 + 数据流向 Mermaid |
| `dependency-map` | "X 调用了什么"、"谁调了 X"、"模块依赖" | call_chain 双向 + 模块依赖图 |
| `external-integrations` | "对接了什么外部系统"、"调了哪些第三方" | 重点画外部依赖 + 标 external classDef |
| `state-transitions` | "状态怎么变的"、"流程节点" | rules 段重点 + 状态机 Mermaid |
| `route-page-map` | "页面怎么连的"（前端场景）| 入口路由表 + 导航图 |
| `command-surface` | CLI 工具 / 命令分发 | 命令树 + 分派逻辑 |
| `pipeline` | 流水线 / 批处理 / ETL | 各阶段拓扑 + 数据流向 |
| `orchestration` | 消息队列 / 事件驱动 | 发布 / 订阅 / broker 拓扑 |
| `storage` | "存了哪些表 / 缓存 / 队列" | 各类存储的角色 + db_ops |
| `business-rule` | "有什么规则 / 限制 / 校验" | rules 段重点展开 |

判断规则：选不准时默认 `overall-architecture`。一个问题只选 1 个主视角。

═════════════════════════════════════════════════════════════
【Step 2：严格规则】
═════════════════════════════════════════════════════════════

1. **不允许编造**：所有方法名、类名、表名必须出自我提供的 context，不能从你的知识里"想当然"
2. **结构化输出**：必须按 6 段式 JSON 输出，缺信息的段落直接省略（不要凑字数）
3. **overview 必出**：哪怕只是说"未找到相关业务逻辑"，也要给一段 overview
4. **简洁专业**：每段 50-200 字，不啰嗦
5. **引用标记**：提到方法/类/表时，用 `[entity_id|显示文本]` 格式（前端会转链接）
6. **中文输出**

═════════════════════════════════════════════════════════════
【Step 3：6 段式结构】
═════════════════════════════════════════════════════════════

- overview     业务概述（必填；开头一行写"视角：xxx"，再 1-2 句业务定位）
- entry_point  入口方法（Controller / API entry 类，附 HTTP 路径）
- call_chain   调用链（5-10 步业务流程；如果适合可附 Mermaid 图）
- db_ops       数据库操作（INSERT/UPDATE/DELETE 哪些表）
- rules        关键约束/业务规则
- sources      引用的代码实体 + 业务文档

═════════════════════════════════════════════════════════════
【Step 4：Mermaid 输出约定（call_chain / 任何含 diagram 的段）】
═════════════════════════════════════════════════════════════

- 节点 ID **必须**用 context 里给出的 entity_id（不能编造）
- 节点标签**两行**：第一行显示名 + `\\n` + 第二行真实路径，例：
    `open-account["OpenAccount\\nsrc/deposit/OpenAccount.java"]`
- 边**必带语义标签**：`A -->|"调用 / 写入 / 校验"| B`（不带标签不写）
- 节点超过 5 个时拆图或加注释，避免"毛球图"
- 4 类预设样式：
    | classDef    | 颜色      | 何时用 |
    | ---         | ---       | --- |
    | `external`  | `#585b70` | 外部系统 / 第三方 API |
    | `entry`     | `#89b4fa` | 入口（HTTP / Controller / CLI / 消费者）|
    | `store`     | `#a6e3a1` | 持久化（DB / 缓存 / 队列）|
    | `concern`   | `#f38ba8` | 已知风险 / 瓶颈 / 待办 |
- Mermaid 写在 `content` 字段里，用 fenced code block：
    ` ```mermaid\\ngraph LR\\n...\\n``` `

═════════════════════════════════════════════════════════════
【Step 5：输出格式（必须合法 JSON，```json fenced 包裹）】
═════════════════════════════════════════════════════════════

```json
{
  "sections": [
    {
      "type": "overview",
      "title": "业务概述",
      "content": "视角：overall-architecture\\n\\n这是...",
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

reference 字段：
  - entity_id:    形如 'method://...' / 'class://...' / 'table://...' / 'doc://...'
  - display_text: 用户友好的显示文本
  - kind:         'method' | 'class' | 'table' | 'doc'

═════════════════════════════════════════════════════════════
【缺信息处理】
═════════════════════════════════════════════════════════════

- 某段没有可靠信息：直接不输出该段（sections 数组少一项即可，不要写空内容）
- 完全没找到相关代码：**仍要出 overview 段**，说明"视角：overall-architecture\\n未找到相关业务逻辑，建议换个说法"
"""


# ─── User prompt 组装函数 ──────────────────────────────────────────────────


# skill_id → 视角偏置提示（一两句话告诉 LLM 优先选哪个 view、答案侧重哪段）
# 故意短小：LLM 看完不会被这一两句"覆盖"掉自己的判断，只在它犹豫时起锚定作用
_SKILL_HINTS: dict[str, str] = {
    "business": "本题已被分类为 business（业务规则）。请优先采用 business-rule 视角；rules 段务必充实，db_ops 段可省略。",
    "dependency": "本题已被分类为 dependency（调用 / 依赖）。请优先采用 dependency-map 或 request-lifecycle 视角；call_chain 段务必含 Mermaid 双向调用图。",
    "data-flow": "本题已被分类为 data-flow（数据流 / 持久化）。请优先采用 data-flow 视角；db_ops 段务必列出所有涉及的表 + 读写操作。",
    "architecture": "本题已被分类为 architecture（整体架构）。请优先采用 overall-architecture 视角；entry_point + call_chain 都要写。",
}


def build_user_prompt(question: str, context: dict[str, Any]) -> str:
    """把 retriever 返回的 context 拼成 LLM user prompt。

    context 结构（来自 RetrievedContext，转 dict）：
      {
        "entry_candidates": [{entity_id, summary_text, level}, ...],
        "callees_by_entry": {entity_id: [callee_id, ...]},
        "callers_by_entry": {entity_id: [caller_id, ...]},
        "table_access_by_entry": {entity_id: [{table_id, operation}, ...]},
        "skill_id": "business" | "dependency" | "data-flow" | "architecture",
      }
    """
    parts: list[str] = []
    parts.append(f"【用户问题】{question}")
    parts.append("")

    # v1.1 路由提示：把 skill_id 翻译成自然语言视角偏置提示
    # 让 LLM 在 Step 1 选视角时更明确
    skill_id = context.get("skill_id") or "architecture"
    skill_hint = _SKILL_HINTS.get(skill_id)
    if skill_hint:
        parts.append("【路由提示】")
        parts.append(skill_hint)
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
    parts.append("先按 system prompt 里的 Step 1 选 1 个主视角，再按该视角侧重组织 6 段式答案。")
    parts.append("严格按 JSON 输出，缺信息段跳过；overview 段无论如何都要出（注明视角）。")
    parts.append("如果 context 不足以回答（比如候选都不相关），")
    parts.append("仍要给一个 overview 段说明：视角：overall-architecture\\n未找到相关业务逻辑，建议换个说法。")

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


# ─── v1.2 chit-chat 闲聊路径的专属 system prompt ─────────────────────────
# 跟 6 段式 SYSTEM_PROMPT 完全分离 — chit-chat 不需要结构化 JSON 输出，
# 也不需要引用约束 / 新鲜度标注，单段友好回复即可。
# 设计：[[chit-chat-闲聊路径-设计]] §4.4

_CHIT_CHAT_SYSTEM = """你是 KE（代码知识工程）的对话助手。用户在和你打招呼、道谢、告别，或询问你能做什么。

回复要求：
1. 简短、友好、自然（1-3 句中文）
2. 如果是问候 / 道谢 / 告别 → 礼貌回应即可
3. 如果是产品问询（「你是谁 / 能做什么 / KE 是什么」）→ 介绍你的 4 个核心能力：
   - 业务规则（约束 / 校验 / 限制）
   - 调用链路（谁调了谁 / 依赖）
   - 数据流（写到哪些表 / 数据如何流转）
   - 整体架构（系统是什么 / 怎么实现）
4. 不要回答与代码工程无关的问题（如天气、新闻、闲聊故事），而是友好地把对话引导回 KE 业务能力上

示例：
- 用户「你好」 → 助手「你好！我是 KE 代码知识工程助手。有关业务规则、调用链路、数据流或架构的问题随时问我。」
- 用户「你是谁」 → 助手「我是 KE（代码知识工程）助手，可以帮你查询代码里的业务规则、调用链路、数据流转和整体架构。」
- 用户「今天天气怎么样」 → 助手「我专注于代码知识查询，天气问题帮不上你。不过工程方面的问题我可以试试 —— 比如某个 Service 的调用链路，或者订单数据是如何流转的。」
"""


# ─── 会话标题总结（v1，2026-05-16）──────────────────────────────────────────
# 首轮问答后异步调用，用首个问题生成一个 ≤15 字的概括性标题。
# 设计：[[会话标题-重命名与智能总结-设计]] §3.2
_TITLE_SUMMARY_SYSTEM = (
    "你是会话标题生成器。请用不超过 15 个汉字概括用户问题的主题，"
    "直接输出标题本身：不要解释、不要引号、不要标点结尾、不要前缀。"
    "若问题是寒暄（你好/在吗等），输出「日常问候」。"
)
