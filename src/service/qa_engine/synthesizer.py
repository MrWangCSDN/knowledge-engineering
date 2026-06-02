"""LLM 合成阶段：把检索到的 context 喂给 LLM，输出 6 段式 JSON。

设计文档：[[首页设计]] §6.1（端到端 sequence）

跨仓依赖：实际 LLM provider 来自主仓 src/llm/factory.py。
本模块用 Protocol 抽象，运行时注入。
"""
from __future__ import annotations

# json：标准库的严格 JSON 解析；遇到未转义引号/反斜杠直接抛 JSONDecodeError
import json
# logging：用 stdlib logging 输出调试 / warn 信息；不引入第三方日志库
import logging
# os：读 KE_CALLCHAIN_AUTO_REPAIR 环境变量，控制 retry 开关（feature flag）
import os
# re：节点 id ASCII 标识符校验
import re
# dataclass 装饰器 + field 工厂：给数据类生成 __init__ / __repr__ 等
from dataclasses import dataclass, field
# Any：可任意类型；Protocol：结构化子类型（duck typing），用来抽象 LLM provider
from typing import Any, Protocol

# 2026-06-02：json-repair 是宽容 JSON 解析器，遇到未转义/截断也能尽力还原；
# 在标准 json.loads 失败后用它作为兜底（详见 _parse_sections）
from json_repair import repair_json

# 模块级 logger；输出 call_chain 修复成功/失败的诊断信息
log = logging.getLogger(__name__)

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

        # 2.5（2026-06-02 新增）：call_chain JSON schema 校验 + 1 次 LLM retry 自修
        # feature flag KE_CALLCHAIN_AUTO_REPAIR（默认 on）；失败时保留原 content 走前端兜底
        sections = await self._repair_call_chain_sections(sections)

        # 3. 估算 token usage（粗算，W8 后端再补真实值）
        approx_tokens = _estimate_tokens(SYSTEM_PROMPT, user_prompt, raw)

        return SynthesizedAnswer(
            sections=sections,
            token_usage=approx_tokens,
            raw_output=raw,
        )

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
        # .lower() 兼容大小写写法；set 形式列出公认的"关"值
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
    # 这两个直接 fail-fast：如果不是 list，后续校验就没意义
    if not isinstance(nodes_raw, list):
        return None, ["'nodes' 字段缺失或不是数组"]
    if not isinstance(edges_raw, list):
        return None, ["'edges' 字段缺失或不是数组"]

    # 5. 逐节点校验
    # 维护 node_ids set 给后续 edges 引用校验用
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
        # from / to 都校验：必须非空字符串 + 引用存在的节点 id
        for end in ("from", "to"):
            ref = e.get(end)
            if not isinstance(ref, str) or not ref:
                errors.append(f"edges[{i}].{end} 缺失或不是非空字符串")
            elif ref not in node_ids:
                errors.append(
                    f"edges[{i}].{end} '{ref}' 引用了 nodes 里不存在的 id（悬挂边）"
                )

    # 有错就返回错误列表；没错就返回 parsed data
    if errors:
        return None, errors
    return data, []


def _build_call_chain_repair_prompt(broken_content: str, errors: list[str]) -> str:
    """构造发给 LLM 的 repair prompt：broken JSON + 错误清单 + schema 提醒。

    设计要点：
      - 错误列表截断前 20 条避免 token 爆（实际不会这么多）
      - schema 给详细字段注释（让 LLM 知道哪些是必填 / 可选）
      - 强调"只输出修复后的 JSON 字面量"——防止 LLM 又包 markdown / 加解释
      - 提供具体修复策略（id 含非法字符怎么办、悬挂边怎么办、kind 非法怎么办）

    Args:
        broken_content: 原始未通过校验的 content 字符串
        errors: _validate_call_chain_content 返回的错误清单

    Returns:
        给 self.llm.complete(user=...) 的 prompt 字符串
    """
    # 截前 20 条（防 token 爆）；每条加 "- " 前缀方便 LLM 区分
    err_list = "\n".join(f"- {e}" for e in errors[:20])
    if len(errors) > 20:
        err_list += f"\n（…还有 {len(errors) - 20} 条错误未列出）"

    # 用 f-string 拼出完整 prompt
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
