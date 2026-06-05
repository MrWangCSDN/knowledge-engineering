"""LLM prompt 模板。

设计原则（详见 [[首页设计]] §5.2 + §10.决策日志）：

1. 强制结构化输出（6 段式 JSON），LLM 不能自由发挥
2. 引用强约束：方法名/类名必须真实存在于 context，不允许编造
3. 新鲜度透明：在 sources 段标注解读生成时间
4. 中文输出
5. 缺信息的段落直接省略（不要凑字数）

v1.4（Plan C4 §7）：新增 AGENT_SYSTEM_PROMPT 自由格式变体供 chat/agent（ReAct）路径——
放开第 1 条的 6 段 JSON 强制（改自然 markdown），第 2 条反幻觉约束仍严格保留；
结构化 6 段（SYSTEM_PROMPT）保留给非 chat 的"结构化拓扑解读"模式。

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
- call_chain   节点-边图（**承载所有图类**：调用链 / 业务流程 / 模块依赖 / 数据流 /
               架构总览图。首选 JSON，前端 ReactFlow 渲染；详见 Step 4）
- db_ops       数据库操作（INSERT/UPDATE/DELETE 哪些表）
- rules        关键约束/业务规则
- sources      引用的代码实体 + 业务文档

═════════════════════════════════════════════════════════════
【Step 4：call_chain 段格式（v1.11 2026-06-02 起首选 JSON，兼容 Mermaid）】
═════════════════════════════════════════════════════════════

call_chain 段承载**所有 nodes-edges 类图**——调用链、业务流程、模块依赖、数据流、
架构总览图都用这同一份 JSON schema 输出。其它段（overview / rules / db_ops / sources）
**不要嵌入图**（这些段是纯文本/markdown）。图集中放 call_chain 段便于交互/全屏/导出。

──【首选】JSON 调用图（前端 ReactFlow 渲染，支持缩放/全屏/PNG 导出/MiniMap）──

call_chain.content 字段直接是合法 JSON 字符串（**不要再包 ```json fence**），schema：

  {
    "nodes": [
      {
        "id": "n1",                                   // 唯一短串（建议 n1/n2/...）
        "label": "OrderController.create",            // 节点显示文本（必填）
        "kind": "controller",                         // controller/service/mapper/method/external
        "classOf": "com.foo.OrderController",         // 类全限定名（可选，hover 显示）
        "sig": "(OrderParam)",                        // 方法签名（可选，hover 显示）
        "filePath": "src/main/java/.../OrderController.java",  // 可选
        "lineNumber": 45,                             // 可选
        "entityId": "method://com.foo.OrderController#create"  // 推荐，用于跳源码
      },
      ...
    ],
    "edges": [
      {"from": "n1", "to": "n2", "label": "调用业务层"}  // label 可选，描述业务动作
    ]
  }

JSON 输出约束：
1. content 就是 JSON 字面量字符串，**不要再包 ```json fence**
2. id 只用 ASCII [a-zA-Z0-9_]，禁止含 `.` / `/` / 空格 / 中文
3. kind 按调用层次填（Controller→controller / Service→service / Mapper→mapper），不确定写 'method'
4. edges.from / edges.to 必须出现在 nodes.id 里，不允许悬挂边
5. nodes 数量 5-15 最佳；超过 20 考虑拆段或筛核心链路
6. 所有字符串用 ASCII 双引号 "，禁止用 弯引号 "" / '' / ｢｣

──【兼容】Mermaid 图（兜底；前端自动识别 ```mermaid fence）──

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
      "content": "[DepositController::openAccount#()|DepositController.openAccount()]\\n  POST /api/account/deposit/open",
      "references": [
        {"entity_id": "DepositController::openAccount#()", "display_text": "DepositController.openAccount()", "kind": "method"}
      ]
    }
  ]
}
```

reference 字段：
  - entity_id:    形如 'ClassName::methodName#(ParamType)'（qualified_name 形态），照搬 candidates 里的值
  - display_text: 用户友好的显示文本
  - kind:         'method' | 'class' | 'table' | 'doc'

═════════════════════════════════════════════════════════════
【缺信息处理】
═════════════════════════════════════════════════════════════

- 某段没有可靠信息：直接不输出该段（sections 数组少一项即可，不要写空内容）
- 完全没找到相关代码：**仍要出 overview 段**，说明"视角：overall-architecture\\n未找到相关业务逻辑，建议换个说法"
"""


# ─── 自由格式 system prompt（v1.4 / Plan C4，设计 §7）────────────────────────
# chat/agent（ReAct）路径用：保留分析师角色 + 视角 + 反幻觉 + 引用标记 + Mermaid 约定，
# 但**不**强制 6 段式 JSON——模型自然 markdown 作答。结构化 6 段能力保留在 SYSTEM_PROMPT
# 给"结构化拓扑解读"非 chat 场景。输出由 _parse_sections 降级分支接住（非 JSON → 单段）。
AGENT_SYSTEM_PROMPT = """你是企业代码知识分析师。你的任务是把代码翻译成业务方/新人能读懂的业务说明，并直接回答用户的问题。

【作答风格】
- 用**自然的 markdown** 作答：按需用标题、列表、表格、代码块（```lang）组织，不必套固定结构。
- 简洁专业、中文；篇幅与问题复杂度匹配，不啰嗦也不凑字数。
- 调用链/架构/数据流类问题，**需要画图时自己调 `render_call_graph` 工具**（图会内联在你说到的位置，可说"见下方调用图"）——绝不手画 mermaid/reactflow。
- **不要给小节顺序编号**（别用「一、二、三」或「1. 2. 3.」当章节号）——用**描述性小标题**（如「## 业务流程」「## 涉及的表」）。调用图由工具内联渲染、不算一个编号小节；否则正文常出现「二、」却没有「一、」的断号。

【严格规则】
1. **不允许编造**：所有方法名、类名、表名必须出自我提供的 context 或工具返回结果，不能从你的知识里"想当然"；宁可说"未找到"也不要虚构 entity_id / 代码内容。
2. **引用标记**：提到方法/类/表时，用 `[entity_id|显示文本]` 格式（前端会转成可点击链接），例：`[DepositController::openAccount#()|DepositController.openAccount()]`。
3. **视角**（可选锚定）：先想清楚用户要的是"整体架构 / 请求流程 / 数据流 / 依赖关系 / 业务规则 / 外部集成"哪一类，据此组织重点，但不必显式声明视角。

【探索流程（重要：判断 context 是否充足）】

判断 context 充足的标准：
  - candidates 数量 ≥ 3 且至少有一个 level 不是 "code_entity" → 充足
  - candidates 全部是 level="code_entity" → 仅代码层数据，**拓扑解读缺失**
  - candidates 为空 → context 严重不足

context 不足时**不要直接放弃**，先用工具探索：

1. **第一步：扩大候选**
   - 不知道叫什么 → ke_search 用问题里的【关键词 / 类名 / 方法名 / 业务词】查
   - 关键词模糊 → 用 ke_glob 找文件名 / ke_grep 找代码常量

2. **第二步：理解候选**
   - 拿到 entity_id → ke_callees / ke_callers 看依赖
   - 想看代码 → ke_read_entity 看 attrs + code_snippet
   - 想看拓扑解读 → ke_method_interp（无解读也 ok，至少有 signature）

3. **第三步：判定是否真的没有**
   - 探索 2-3 轮后仍无有用结果 → 输出"我尝试了 ke_search('xxx') / ke_callees(yyy) 等工具，未能找到符合的 entity。建议补充：1) 完整类全限定名 2) 业务关键词 3) entity_id"
   - **不要无尝试就投降**

特殊情况：candidates 全是 level="code_entity"（拓扑解读缺失）：
  - 说明此工程只跑了代码索引，没跑拓扑解读
  - 你能基于代码本身解读：方法签名、调用关系、SQL preview（MyBatis）等
  - **不要**因 summary_text 为空就说"未找到"——代码层数据已经足够给出有意义的回答

【画图约定 —— 唯一规则，必须遵守】

任何"节点-边"类图（调用链 / 业务流程 / 模块依赖 / 架构总览 / 数据流）**一律调
`render_call_graph(entity_id, direction)` 工具**出图。工具自动生成 ReactFlow 图：准确、
可缩放/全屏/PNG 导出、节点可点击跳源码、自带中文业务标签，直接内联渲染。

⚠️ **严禁自己手画任何格式的图**：不要输出 mermaid 代码块、不要输出 reactflow 代码块、
不要用 ASCII 画框线图。手画的边常臆造、且前端无法稳定渲染（会"解析失败"显示裸代码）。

- 出图时机：用户问"怎么实现 / 流程 / 调用链 / 架构 / 依赖 / 数据流"等，先调 render_call_graph，
  再用文字解释；正文里只说"见下方调用图"，不要逐节点复述。
- entity_id 用 context 候选或「调用链方法清单」里的真实值（带 method:// 前缀的照搬）；
  direction 拿不准用 down，工具会自动回退到有调用关系的一侧。
- 工具返回空（该入口无调用边）→ 就不画图、改用文字/表格说明，**绝不退回手画**。
"""


# ─── User prompt 组装函数 ──────────────────────────────────────────────────


# skill_id → 视角偏置提示（一两句话告诉 LLM 优先选哪个 view、答案侧重哪段）
# 故意短小：LLM 看完不会被这一两句"覆盖"掉自己的判断，只在它犹豫时起锚定作用
_SKILL_HINTS: dict[str, str] = {
    "business": "本题已被分类为 business（业务规则）。请优先采用 business-rule 视角；rules 段务必充实，db_ops 段可省略。",
    "dependency": "本题已被分类为 dependency（调用 / 依赖）。请优先采用 dependency-map 或 request-lifecycle 视角；务必给出双向调用图（机制按各自路径：agent 调 render_call_graph 工具，6 段出 call_chain 段）。",
    "data-flow": "本题已被分类为 data-flow（数据流 / 持久化）。请优先采用 data-flow 视角；db_ops 段务必列出所有涉及的表 + 读写操作。",
    "architecture": "本题已被分类为 architecture（整体架构）。请优先采用 overall-architecture 视角；entry_point + call_chain 都要写。",
}


# 快赢 B（[[召回链路缺陷诊断与修复方案]]）：渲染给 LLM 的候选入口上限。
# 5→10：配合召回 top_k 调大，让更多入口/链路成员进入作答上下文，流程图才完整。
TOP_CANDIDATES_FOR_PROMPT = 10


def build_user_prompt(question: str, context: dict[str, Any], free_format: bool = False) -> str:
    """把 retriever 返回的 context 拼成 LLM user prompt。

    context 结构（来自 RetrievedContext，转 dict）：
      {
        "entry_candidates": [{entity_id, summary_text, level}, ...],
        "callees_by_entry": {entity_id: [callee_id, ...]},
        "callers_by_entry": {entity_id: [caller_id, ...]},
        "table_access_by_entry": {entity_id: [{table_id, operation}, ...]},
        "skill_id": "business" | "dependency" | "data-flow" | "architecture",
      }

    free_format: True → 尾部任务块用自然 markdown 指引（chat/agent 路径，Plan C4 §7）；
                 False（默认）→ 原结构化 6 段 JSON 指令（QASynthesizer，向后兼容）。
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
    # source-first grounding P1（[[业务问答-源码优先接地-P1设计]]）：候选真实源码 {entity_id: 源码}
    code_snippets = context.get("candidate_code_snippets") or {}
    if candidates:
        parts.append("")
        parts.append("候选入口方法（按相关度倒序）:")
        # 框架句：仅当至少有一个候选附了真实源码时加，重定位"代码细节以源码为准、2b 解读仅业务提示"
        if code_snippets:
            parts.append(
                "  （注：以下候选凡附【真实源码片段】的，代码细节——SQL/表名/字段/存储技术/方法调用/状态码"
                "——一律以源码为准，2b 业务说明仅作业务提示、不可当代码事实；引用仍用 entity_id。）"
            )
        # 取前 N 个候选渲染给 LLM（快赢 B：上限 TOP_CANDIDATES_FOR_PROMPT=10）。
        # 渲染循环与下方模块指引守卫共用同一份切片，避免两处不同步（DRY）。
        top_candidates = candidates[:TOP_CANDIDATES_FOR_PROMPT]
        for i, c in enumerate(top_candidates, 1):
            entity_id = c.get("entity_id", "?")
            level = c.get("level", "method")
            # 取 module 字段（可能为 None 或根本不存在）；.get() 缺失键时返回 None
            module = c.get("module")
            summary = c.get("summary_text") or "(无业务说明)"
            # 截断过长的 summary 控制 token 数
            if len(summary) > 300:
                summary = summary[:300] + "…"
            # 仅当 module 有值（非 None / 非空串）才拼 (模块: x)，避免 None 渲染成噪声
            # f-string 三元表达式：`a if 条件 else b` 是 Python 内联条件表达式
            mod_str = f"  (模块: {module})" if module else ""
            parts.append(f"  {i}. {entity_id}  [level={level}]{mod_str}")
            parts.append(f"     业务说明: {summary}")
            # source-first grounding P1：该候选预读到真实源码 → 附上（代码细节以此为准）
            snippet = code_snippets.get(entity_id)
            if snippet:
                parts.append("     【真实源码片段】(代码细节以此为准):")
                parts.append("     ```")
                # 逐行缩进对齐候选块（splitlines 按行切，不保留行尾换行）
                for line in snippet.splitlines():
                    parts.append(f"     {line}")
                parts.append("     ```")
        # 模块判断指引：仅当至少一个候选带有 module 时追加，让 LLM 按 module 判前台/后台
        # any() 是内置函数，可迭代对象有任意一个真值即返回 True
        if any(c.get("module") for c in top_candidates):
            parts.append(
                "  （模块说明：mall-portal=前台门户、mall-admin=后台管理、mall-search=搜索、"
                "mall-auth=认证、mall-gateway=网关。判断前台/后台等归属请按上面标注的【模块】，"
                "不要仅凭类名/方法名臆断；若要对比的另一侧模块不在候选里，"
                "如实说\"未检索到 X 模块的相关实体\"。）"
            )
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

    # 2b. 多跳调用链路（C2 [[召回链路缺陷诊断与修复方案]]）：保留 from→to 边，供 LLM 画 call_chain 调用图。
    # 1 跳骨架太空会让 LLM 省略 call_chain；这里给多跳边 + 明确提示"据此画"，让流程图类问题能出 ReactFlow。
    call_edges = context.get("call_edges_by_entry") or {}
    if any(call_edges.values()):
        parts.append("")
        if free_format:
            parts.append("调用链路（入口向下多跳展开，from → to 边）—— 需要调用图时调 render_call_graph 工具"
                         "（传入口 entity_id），不要手画；以下边供你理解调用结构:")
        else:
            parts.append("调用链路（入口向下多跳展开，from → to 边）—— 画调用图/流程图时请据此输出 call_chain 段:")
        for entry, edges in call_edges.items():
            if not edges:
                continue
            parts.append(f"  入口 {entry}:")
            for frm, to in edges:
                parts.append(f"      {frm}  →  {to}")

    # 2c. 逻辑图中文化（[[逻辑图中文化-设计]] §4.2）：调用链方法的 2b 中文业务解读 + A1 业务流指令。
    # 让 LLM 把上面的方法调用链「重抽象」成中文业务流程图——每个业务步骤节点锚定一个真实方法。
    node_summaries = context.get("callchain_node_summaries") or {}
    if node_summaries:  # 有任何 2b 业务解读才推 A1（否则信号太弱，交确定性兜底图）
        # 收集调用链真实方法集（call_edges 端点，按出现顺序去重）——A1 锚定的「合法全集」。
        # 关键：列**全部**召回方法（含无解读的），不只列有解读的——否则 LLM 看到"可锚定集(3)"与
        # "调用链(11)"矛盾 → 干脆不产 call_chain → 回退英文方法图（实测踩坑）。
        recalled_methods: list[str] = []
        seen_m: set[str] = set()
        for edges in call_edges.values():
            for frm, to in edges:
                for m in (frm, to):
                    if m not in seen_m:
                        seen_m.add(m)
                        recalled_methods.append(m)
        parts.append("")
        parts.append("调用链方法清单（含业务解读；引用 / 画图只能锚定这些真实方法的 entityId，严禁虚构）:")
        for m in recalled_methods:
            s = node_summaries.get(m)
            if s:
                parts.append(f"  - entityId: method://{m} | 方法: {m} | 业务解读: {s}")
            else:
                parts.append(f"  - entityId: method://{m} | 方法: {m} | （无解读，按方法名+签名理解）")
        # 画图指令分路径：agent（free_format）一律调 render_call_graph 工具、绝不手画；
        # 6 段沿用 A1 锚定式手绘 call_chain JSON 段（QASynthesizer 一次性合成，无工具）。
        parts.append("")
        if free_format:
            parts.append("【需要调用图 / 业务流程图时】调 `render_call_graph` 工具（用上面清单里的真实 entityId 作入口），"
                         "工具自动出可点击的 ReactFlow 图——**不要自己手画 mermaid / reactflow**。")
        else:
            parts.append("【画 call_chain 业务流程图时（A1 锚定式）】")
            parts.append("  1. 把上述调用链重写成中文业务步骤流：可把连续的几个方法合并成一个业务步骤；")
            parts.append("  2. 每个节点必须锚定到上面清单里的某个真实方法，entityId 照搬其 method:// 值（点击可跳源码）；")
            parts.append("  3. label 用中文业务动作（≤12 字）；有「业务解读」的据其提炼，无解读的按方法名/签名理解；")
            parts.append("  4. edge.label 用中文衔接（如「校验通过后」「下单成功触发」）；")
            parts.append("  5. 只能用上面清单里的方法，严禁虚构代码里没有的步骤/方法。")

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
    if free_format:
        # 自由格式（chat/agent，设计 §7）：自然 markdown，不套 6 段
        parts.append("用自然的 markdown 作答（标题/列表/代码块按需），不必套固定结构。")
        parts.append("提到方法/类/表时用 `[entity_id|显示文本]` 标注；只能基于 context/工具返回的真实实体，不得编造 entity_id。")
        # 画图：agent 自主调 render_call_graph 工具内联出图（Task 4 进一步强化双模式指引）
        parts.append("如本题适合调用图/流程图/架构图，**自己调 `render_call_graph` 工具**出图（图会内联在你说到的位置），不要手画 mermaid/reactflow。")
        # v1.5 ReAct 代码层兜底：context 不足时**不要直接放弃**，按 system prompt 的「探索流程」
        # 主动调 ke_search / ke_callees 等工具补齐；探索 2-3 轮仍无果再按"第三步"格式输出。
        # 详见 [[ReAct-代码层兜底-设计]] §4
        parts.append("如果 candidates 不足，按 system prompt 里的「探索流程」主动调用工具补齐再回答；探索仍无果时再说明并建议补充。")
    else:
        # 结构化 6 段（非 chat / QASynthesizer，保持原样）
        parts.append("先按 system prompt 里的 Step 1 选 1 个主视角，再按该视角侧重组织 6 段式答案。")
        parts.append("严格按 JSON 输出，缺信息段跳过；overview 段无论如何都要出（注明视角）。")
        parts.append("如果 context 不足以回答（比如候选都不相关），")
        parts.append("仍要给一个 overview 段说明：视角：overall-architecture\\n未找到相关业务逻辑，建议换个说法。")

    return "\n".join(parts)


# ─── 多轮对话上下文压缩（v1 暂未启用，保留接口）──────────────────────────

HISTORY_SUMMARIZE_PROMPT = """以下是用户之前的对话历史。请用 1-2 句话概括重点，作为后续对话的上下文：

{history}

概括："""


def _format_history(history: list[dict] | None) -> str:
    """把最近 ≤10 轮历史格式化为多行 `[role] content(≤200字)`。

    KG 与 chit-chat 共用单一来源（DRY）。防御：history 非 list/None/空 → ""；
    非 dict 项跳过；dict 但 role/content 值为 None（键在、值 None，dict.get 默认
    不生效）→ 用 '?' / '' 兜底——否则 None[:200] 抛 TypeError（content=null 的
    出错/结构化消息很常见）。正常全 dict 时输出与既有逐字节一致。
    """
    if not isinstance(history, list) or not history:
        return ""
    lines: list[str] = []
    for m in history[-10:]:
        if not isinstance(m, dict):
            continue
        lines.append(f"[{m.get('role') or '?'}] {(m.get('content') or '')[:200]}")
    return "\n".join(lines)


def build_chitchat_user_prompt(
    question: str, history: list[dict] | None = None
) -> str:
    """chit-chat 专属 user prompt：带最近历史（无 KG 6 段脚手架）。

    history 空/None → 仅 question（保持 chit-chat 无历史时旧行为，逐字节一致）。
    设计：[[记忆系统-设计]] §20。
    传入的 history 通常已由 router 按 §18 token 预算裁过；此处 _format_history 的 [-10:] 仅冗余兜底。
    """
    h = _format_history(history)
    if not h:
        return question
    return f"【对话历史】\n{h}\n\n{question}"


def build_user_prompt_with_history(
    question: str,
    context: dict[str, Any],
    history: list[dict] | None = None,
) -> str:
    """把历史轮直接拼到 question 前面（不在此压缩）。

    P2②（[[记忆系统-设计]] §18）起：router 进流前已按模型窗口 token 预算裁过
    body.history（更早轮由 system 记忆块头部的 session summary 顶替，S5 在 qa_router 5b/5c 读侧 composer 实现），传入此处
    的已是裁好的最近若干轮。此处的 history[-10:] 仅作冗余兜底硬上限，正常不会触发。
    """
    if not history:
        return build_user_prompt(question, context)

    base = build_user_prompt(question, context)
    return f"【对话历史】\n{_format_history(history)}\n\n{base}"


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

_CHIT_CHAT_SYSTEM = """你是 KE（代码知识工程）的对话助手，同时也是一个有用的编程/技术助手。

回复原则：
1. 问候 / 道谢 / 告别 → 简短、友好、自然（一两句即可）
   **【关键】**即使记忆 / 历史 / session summary 中有之前的话题或未完成任务，
   也**绝不**在简单问候里主动延续 / 复述 / 续写 — 用户说「你好 / 在吗 / 谢谢」就只回应这句问候本身。
   等用户**明确**说「继续刚才的话题 / 接着写 / 上次说到哪了」时才接续。
   反例：✗「你好，张三！刚刚你想看 Java 排序，我来帮你继续写...」
   正例：✓「你好，张三！有什么我可以帮你的吗？」
2. 产品问询（「你是谁 / 能做什么 / KE 是什么」）→ 介绍你的 4 个核心能力（针对用户已接入的代码库）：
   - 业务规则（约束 / 校验 / 限制）
   - 调用链路（谁调了谁 / 依赖）
   - 数据流（写到哪些表 / 数据如何流转）
   - 整体架构（系统是什么 / 怎么实现）
3. 通用编程 / 技术问题（如「用 Java 写个排序」「解释下快排」「什么是闭包」）→ 像专业编程助手一样**直接、完整地回答**：可用 Markdown、代码块，长度按需要来，不必刻意简短，也不要推脱或搪塞
4. 与技术完全无关的问题（天气 / 八卦 / 闲聊故事）→ 简短自然地回应，可以顺口提一句你更擅长分析代码库，但语气轻松，别生硬地背诵能力清单

整体语气：自然、专业、有帮助。绝不用「我专注于代码知识查询，比如业务规则、调用链路、数据流和架构」这种死板套话去搪塞具体的技术问题。

【关于用户的身份 / 姓名 / 偏好 / 个人信息】信源优先级（务必严格遵循）：
- 第一信源：system 顶部的【记忆】块（标有"═══ 记忆（关于本用户 / 本次会话的已知事实，优先参考）═══"分隔线）。
  此块内任何以 "## identity" "## preference" "## style_feedback" 开头的段、
  以及形如 "- 用户名叫X" / "- 用户喜欢Y" 的 bullet 都是**已确认的用户事实**，
  必须优先采信。看到记忆里写"用户名叫张三"就答"你叫张三"，**绝不**再说"你还没告诉我"。
- 第二信源：本轮【对话历史】里用户**自己说过**的事实（如 "我叫X" "我喜欢Y"）
- 都没有时才说『你还没告诉我』或『我不知道』，并礼貌邀请用户告知；**绝不**猜测 / 编造 / 把代词「用户」「user」「你」当成名字 / 根据问题主题反推身份

示例：
- 用户「你好」→「你好！有什么编程或代码方面的问题都可以问我。」
- 用户「你能做什么」→「我是 KE 代码知识工程助手。除了回答通用编程问题，我更擅长分析你已接入的代码库——业务规则、调用链路、数据流转、整体架构都能问我。」
- 用户「用 Java 写个冒泡排序」→（直接给出带注释的完整 Java 代码 + 简要说明，不要推脱）
- 用户「今天天气怎么样」→「天气我看不了，不过编程或者你代码库的问题都可以找我聊。」
- 用户「我叫什么名字」+ 记忆里写"用户名叫张三" → 直接答「你叫张三。」（依据记忆事实，不要兜底为"没告诉我"）
- 用户「我叫什么名字」+ 记忆 / 历史都没相关信息 → 才回「你还没告诉我你的名字～可以告诉我一声。」
"""


# ─── 会话标题总结（v1，2026-05-16）──────────────────────────────────────────
# 首轮问答后异步调用，用首个问题生成一个 ≤15 字的概括性标题。
# 设计：[[会话标题-重命名与智能总结-设计]] §3.2
_TITLE_SUMMARY_SYSTEM = (
    "你是会话标题生成器。请用不超过 15 个汉字概括用户问题的主题，"
    "直接输出标题本身：不要解释、不要引号、不要标点结尾、不要前缀。"
    "若问题是寒暄（你好/在吗等），输出「日常问候」。"
)


# ─── 记忆系统 P1（2026-05-16）──────────────────────────────────────────────
# 设计：[[记忆系统-设计]] §7。记忆块注入 system prompt 顶部（优先级最高，
# 早于角色与规则），让模型先读「人类真相」再按既有规则作答。

_MEMORY_BLOCK_TEMPLATE = (
    "═══════ 记忆（关于本用户 / 本次会话的已知事实，优先参考）═══════\n"
    "{block}\n"
    "═══════════════════════════════════════════════════════════════\n\n"
)


def with_memory_block(system: str, memory_block: str | None) -> str:
    """把召回的记忆块拼到 system prompt 最前面。

    memory_block 为 None / 全空白 → 原样返回 system（零开销、行为不变）。
    """
    if not memory_block or not memory_block.strip():
        return system
    return _MEMORY_BLOCK_TEMPLATE.format(block=memory_block.strip()) + system


# 会话级压缩：把最近若干轮对话压成一段「工作状态」（本次目标/已确认/已排除）。
# 设计：[[记忆系统-设计]] §4.3 会话级。
_SESSION_COMPACT_SYSTEM = (
    "你是对话记忆压缩器。基于【已有会话摘要】（若有）与【新增对话】，输出一段"
    "更新后的会话摘要，忠实保留对后续有用的关键信息：用户陈述的事实与偏好"
    "及其先后/演变时间线、已确认的结论、当前状态、未决问题。"
    "不得丢弃【已有会话摘要】中的既有事实——把新信息融合进去，有变化则标注演变。"
    "不超过 300 字，中文，直接输出摘要正文，不要前缀、不要解释、不要分点编号。"
)


# ─── 文件式记忆 S2：L0/L1 自底向上生成（2026-05-19）────────────────────────
# 设计：[[文件式记忆重构-设计]] §3.5。L0=可嵌入摘要（≤100 tok，S3 向量检索
# 目标）；L1=导航图（≤1–2k tok）。两常量就近置此（与会话压缩/意图解析同处）。
_MEM_L0_SYSTEM = (
    "你是记忆摘要器。把给定文本压成一句可独立检索的中文摘要："
    "聚焦关于本用户的稳定事实 / 偏好 / 身份，不超过约 100 token，"
    "不要前缀、不要解释、不要分点编号，直接输出摘要正文本身。\n"
    "【关键约束 — 严禁虚构】只能复述【输入文本中已经明确出现】的事实。"
    "禁止补充、推测、扩写、关联任何输入未提及的信息（如年龄/籍贯/职业/家庭/学历/兴趣等"
    "若输入未写就一律不准出现）。如输入只是「张三」一个名字，"
    "输出也只能围绕「张三」这个名字本身，绝不许编出生年份、城市、爱好等。"
    "事实越少摘要越短是正常的。"
)


_MEM_L1_SYSTEM = (
    "你是记忆导航图生成器。给你某目录下若干子项的摘要"
    "（以「## 子项名」分节）。聚成一张导航图：有哪些记忆条目、"
    "各自讲什么、需要时如何进一步查看其正文。中文，不超过约 1500 字，"
    "结构清晰可作为该子树索引；直接输出导航图正文，不要前缀、不要额外解释。\n"
    "【关键约束 — 严禁虚构】只能复述【输入子项摘要中已经明确出现】的事实。"
    "禁止补充、推测、扩写、关联任何输入未提及的信息（如生平、教育、职业、家庭、社交、未来规划等"
    "若输入未写就一律不准出现）。子项内容稀少时，导航图也应相应简短；"
    "事实越少导航图越短是正常的、可接受的。"
)


# ─── 文件式记忆 S4：ReAct 抽取（2026-05-21）──────────────────────────────
# 设计：[[文件式记忆重构-设计]] §5.2。单次 LLM 调用，输出 JSON 数组含 kind /
# content / supersedes_kind 三字段。空 {"memories":[]} = 本轮无可记。
_MEM_EXTRACT_SYSTEM = (
    "你是用户记忆抽取器。给你一段用户与助理的对话，抽取所有值得长期记住的"
    "关于本用户的事实，分类为 preference / identity / style_feedback：\n"
    "- identity：用户身份/姓名/自我称呼/角色（必含 supersedes_kind='identity'，"
    "  会取代旧身份事实，先更新不并存重复，避免「王山河→李龙飞」类 bug）；\n"
    "- preference：用户长期偏好（语言、风格、领域、工程范畴等）；\n"
    "- style_feedback：用户对回答风格/格式/长度的反馈；\n"
    "【关键约束 1 — 信源】只抽取**用户**【明确声明】的事实"
    "（「我叫X」「记住我喜欢Y」「我是Z角色」等用户陈述句）。"
    "【绝不】把助理回复里的内容当作用户事实抽取——"
    "助理可能猜测、幻觉、复述（如「你是北京的软件工程师」可能是 LLM 编的，并非用户说过）。"
    "助理回复的内容**仅供你理解上下文**，不可作为事实来源。\n"
    "【关键约束 2 — 句式】疑问/澄清/引用不是事实——"
    "「我叫什么」「我的名字是什么」「你刚才说X」「你也知道啊」等返回空数组。\n"
    '输出严格 JSON：{"memories":[{"kind":...,"content":"第三人称陈述事实",'
    '"supersedes_kind":null|"identity"}]}。'
    "kind=identity 时 supersedes_kind 必须填 'identity'，禁止填 null。"
    '本轮无可记则 {"memories":[]}。'
    "只输出 JSON 对象本身，不要代码块、不要解释。"
)
