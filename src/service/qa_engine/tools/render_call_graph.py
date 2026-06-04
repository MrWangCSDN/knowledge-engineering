"""render_call_graph 工具：渲染类工具（副作用型）。

与调查类工具（ke_callees 等）的区别：
  - 调查类：返回数据全部回灌 LLM 上下文
  - 渲染类：图数据只走前端内联渲染；回灌 LLM 的只有一句 summary（省 token、防模型用文字复述图）
        → ReActSynthesizer 看到结果含 "render" 字段时，只把 summary 写进 LLM tool message（见 react_synthesizer 改造）

复用 synthesizer 的确定性调用图构建（_build_call_chain_section_from_edges），产出与
前端 CallChainFlow / tryParseCallChain 同构的 {nodes, edges}。
"""
from __future__ import annotations

# deque：BFS 队列（同 ke_impact 的遍历范式）
from collections import deque
# json：把 _build_call_chain_section_from_edges 的 content(JSON 字符串)解析回 dict 装进 render
import json
# typing：Callable/Optional 声明可选的 summary_lookup 注入；Any 占位 input/output dict 的值类型
from typing import Any, Callable, Optional

# GraphProto：注入式图后端协议（与 ke_callees/ke_impact 同源，零依赖主仓实现）
from src.service.qa_engine.retriever import GraphProto
from src.service.qa_engine.tools.base import Tool
# 复用 synthesizer 既有的确定性调用图构建 + 短名工具（模块级函数，可直接 import）
from src.service.qa_engine.synthesizer import _build_call_chain_section_from_edges, _cc_label

# 默认/上限：防超大图把渲染跑爆（与 ke_impact 同口径）
_DEFAULT_DEPTH = 2
_MAX_DEPTH = 4
_MAX_EDGES = 60

# input_schema：MCP 兼容；entity_id 必填，direction/depth 选填
_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entity_id": {
            "type": "string",
            "description": "起始实体 ID，形如 OmsPortalOrderServiceImpl::generateOrder#(OrderParam)",
        },
        "direction": {
            "type": "string",
            "description": "down=下游调用图（它调用了谁）；up=上游调用图（谁调用它）",
            "enum": ["down", "up"],
            "default": "down",
        },
        "depth": {
            "type": "integer",
            "description": "遍历跳数",
            "default": _DEFAULT_DEPTH,
            "minimum": 1,
            "maximum": _MAX_DEPTH,
        },
    },
    "required": ["entity_id"],
}


def build_render_call_graph_tool(
    graph: GraphProto,
    *,
    summary_lookup: Optional[Callable[[str], str]] = None,
) -> Tool:
    """构造绑定到指定 GraphProto 的 render_call_graph 工具。

    :param graph: 图后端（同 ke_callees/ke_impact）
    :param summary_lookup: 可选，entity_id → 2b 中文解读（用于中文 label）；
        None 时 label 回退方法短名（仍可用，只是非中文业务名）。
    """

    # handler 是 async function；闭包捕获 graph / summary_lookup（Python 注入式编程惯用法）
    async def handler(input: dict[str, Any]) -> dict[str, Any]:
        # 校验必填：缺 entity_id → 返回错误信号（render=None，LLM 看到 error 不会以为渲染成功）
        entity_id = input.get("entity_id")
        if not entity_id:
            return {"render": None, "summary": "缺少 entity_id，无法渲染调用图", "error": "missing required field: entity_id"}

        # direction 兜底：非 'up' 一律 'down'
        direction = "up" if input.get("direction") == "up" else "down"
        # depth 容错（LLM 可能传 string）+ 夹 [1, _MAX_DEPTH]
        try:
            depth = int(input.get("depth", _DEFAULT_DEPTH))
        except (TypeError, ValueError):
            depth = _DEFAULT_DEPTH
        depth = max(1, min(depth, _MAX_DEPTH))

        # BFS 收集"边"的内部函数（与 ke_impact 同范式；统一成"调用方→被调用方"方向）
        def _collect(dir_: str) -> tuple[list[tuple[str, str]], set[str]]:
            neighbors = graph.successors if dir_ == "down" else graph.predecessors
            edges_: list[tuple[str, str]] = []
            seen_e: set[tuple[str, str]] = set()
            seen_n: set[str] = {entity_id}
            queue: deque[tuple[str, int]] = deque([(entity_id, 0)])
            while queue:
                node, d = queue.popleft()
                if d >= depth:
                    continue
                for nxt in neighbors(node):
                    frm, to = (node, nxt) if dir_ == "down" else (nxt, node)
                    if (frm, to) in seen_e:
                        continue
                    seen_e.add((frm, to))
                    edges_.append((frm, to))
                    if nxt not in seen_n:
                        seen_n.add(nxt)
                        queue.append((nxt, d + 1))
                    if len(edges_) >= _MAX_EDGES:
                        return edges_, seen_n
            return edges_, seen_n

        # 先按请求方向收集；若该方向无边（典型：Controller 无上游 / Dao 无下游，候选节点上下游不对称），
        # 自动回退反方向，避免"选错方向→空图→agent 手画兜底"。direction 改写为实际命中的方向（summary 用）。
        try:
            edges, seen_nodes = _collect(direction)
            if not edges:
                other = "up" if direction == "down" else "down"
                e2, n2 = _collect(other)
                if e2:
                    edges, seen_nodes, direction = e2, n2, other
        except Exception as e:
            # 图后端挂了 → 错误信号，agent 改用文字描述、不崩
            return {"render": None, "summary": "图后端异常，未生成调用图", "error": f"graph backend error: {e}"}

        if not edges:
            return {"render": None, "summary": f"未找到 {_cc_label(entity_id)} 的调用关系"}

        # 取节点中文解读（可选）：summary_lookup 逐个查，拼成 _build_call_chain_section_from_edges 要的 node_summaries
        summaries: dict[str, str] = {}
        if summary_lookup is not None:
            for nid in seen_nodes:
                try:
                    s = summary_lookup(nid)
                except Exception:
                    s = ""
                if s:
                    summaries[nid] = s

        # 复用确定性构建：传 {entity_id: edges}（_build_call_chain_section_from_edges 的 call_edges_by_entry 形态）
        section = _build_call_chain_section_from_edges({entity_id: edges}, node_summaries=summaries)
        if not section:
            # 全是框架噪声（getter/Example/CRUD）被滤光 → 不渲染空图
            return {"render": None, "summary": f"{_cc_label(entity_id)} 的调用关系均为框架噪声，未生成图"}

        # section["content"] 是 JSON 字符串，解析回 dict 放进 render.data（前端 CallChainFlow 直接吃）
        data = json.loads(section["content"])
        n = len(data.get("nodes", []))
        flow = "下游" if direction == "down" else "上游"
        return {
            "render": {"kind": "call_graph", "data": data},
            # 只此一句回灌 LLM：模型知道"图已渲染"，自然衔接"见下方调用图"，不再用文字复述
            "summary": f"已渲染 {_cc_label(entity_id)} 的{flow}调用图（{n} 节点）",
        }

    # 把 handler 和元数据打包成 Tool
    return Tool(
        name="render_call_graph",
        description=(
            "渲染调用关系图（可视化）。当问题涉及'调用链路/流程/它调了谁/谁调它'时调用，"
            "在答案里内联生成一张可点击的调用图。direction=down 下游、up 上游。"
            "图直接展示给用户，你只需在文字里自然提及'见下方调用图'，不要用文字复述图里的节点。"
        ),
        input_schema=_SCHEMA,
        handler=handler,
    )
