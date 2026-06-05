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
# 复用 synthesizer 既有的确定性调用图构建 + 短名工具 + 节点上限（模块级，可直接 import）
from src.service.qa_engine.synthesizer import (
    _build_call_chain_section_from_edges,
    _cc_label,
    _CALLCHAIN_MAX_NODES,
)

# 默认/上限：防超大图把渲染跑爆（与 ke_impact 同口径）
_DEFAULT_DEPTH = 2
_MAX_DEPTH = 4
_MAX_EDGES = 60

# input_schema：MCP 兼容。两种模式二选一——
#   模式 A（代码调用图）：传 entity_id [+direction/depth]，从真实方法 BFS 出调用关系图。
#   模式 B（freeform 逻辑/架构图）：传 nodes[]+edges[]，agent 直接给出任意节点-边图。
# entity_id 不再 required（handler 内做"二选一"校验）。
_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entity_id": {
            "type": "string",
            "description": "【模式A】起始实体 ID，形如 OmsPortalOrderServiceImpl::generateOrder#(OrderParam)",
        },
        "direction": {
            "type": "string",
            "description": "【模式A】down=下游调用图（它调用了谁）；up=上游调用图（谁调用它）",
            "enum": ["down", "up"],
            "default": "down",
        },
        "depth": {
            "type": "integer",
            "description": "【模式A】遍历跳数",
            "default": _DEFAULT_DEPTH,
            "minimum": 1,
            "maximum": _MAX_DEPTH,
        },
        "nodes": {
            "type": "array",
            "description": "【模式B】节点列表，画任意业务逻辑/架构图。每项 {id, label(中文业务名), code(英文 类.方法,可选), kind(controller/service/dao/method,可选)}",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "label": {"type": "string"},
                    "code": {"type": "string"},
                    "kind": {"type": "string"},
                },
                "required": ["id", "label"],
            },
        },
        "edges": {
            "type": "array",
            "description": "【模式B】边列表，每项 {source, target, label(可选)}（端点用 nodes 里的 id）",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "target": {"type": "string"},
                    "label": {"type": "string"},
                },
                "required": ["source", "target"],
            },
        },
    },
}


def _build_freeform_graph(nodes_in: Any, edges_in: Any) -> dict[str, Any]:
    """模式 B：用 agent 给的 nodes/edges 直接构造 reactflow 图（业务逻辑/架构图，不触图后端）。

    输出与模式 A（``_build_call_chain_section_from_edges``）**同构**，前端零改动即可吃：
      - node = ``{id, label(中文业务名), method(英文代码标识), kind, [entityId]}``
      - edge = ``{from, to, [label]}``（统一成 from/to，非 source/target）

    :param nodes_in: agent 给的节点列表，每项 ``{id, label, code?, kind?, entityId?}``
    :param edges_in: agent 给的边列表，每项 ``{source/from, target/to, label?}``
    :return: ``{render:{kind:'call_graph', data:{nodes,edges}}, summary}``；无有效节点 → error 信号
    """
    # 节点归一化 + 截断（控图大小，与模式 A 同口径 _CALLCHAIN_MAX_NODES）
    out_nodes: list[dict[str, Any]] = []
    valid_ids: set[str] = set()                          # 已收节点 id 集，用于校验边端点
    for nd in (nodes_in or [])[:_CALLCHAIN_MAX_NODES]:
        if not isinstance(nd, dict):                     # LLM 是系统边界，非 dict 项跳过
            continue
        nid = nd.get("id")
        if not nid or str(nid) in valid_ids:             # 缺 id / 重复 → 跳过
            continue
        nid = str(nid)
        valid_ids.add(nid)
        # input 用 code 表示英文代码标识 → 输出字段名 method（对齐前端 MethodNode 读 data.method）
        node: dict[str, Any] = {
            "id": nid,
            "label": str(nd.get("label") or nid),                       # 中文业务名（缺则 id 兜底）
            "method": str(nd.get("code") or nd.get("method") or ""),    # 英文 类.方法
            "kind": str(nd.get("kind") or "method"),                    # 分层角色（前端着色）
        }
        ent = nd.get("entityId") or nd.get("entity_id")  # 若该节点是真实方法 → 带 method:// 让前端可点击跳源码
        if ent:
            ent = str(ent)
            node["entityId"] = ent if ent.startswith("method://") else f"method://{ent}"
        out_nodes.append(node)

    if not out_nodes:                                    # 无有效节点 → error 信号，agent 改文字、不崩
        return {"render": None, "summary": "nodes 为空，未渲染图", "error": "freeform needs non-empty nodes"}

    # 边归一化：接受 from/to 或 source/target → 统一 {from,to}；丢悬挂边（端点不在节点集）+ 去重 + 截断
    out_edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for ed in (edges_in or []):
        if not isinstance(ed, dict):
            continue
        frm = ed.get("from") or ed.get("source")         # 两种命名都收（agent 习惯 source/target）
        to = ed.get("to") or ed.get("target")
        if not frm or not to:
            continue
        frm, to = str(frm), str(to)
        if frm not in valid_ids or to not in valid_ids:  # 悬挂边 → 丢（防前端渲染崩）
            continue
        if (frm, to) in seen:                            # 去重
            continue
        seen.add((frm, to))
        edge: dict[str, Any] = {"from": frm, "to": to}
        if ed.get("label"):                              # 可选边标签（前端忽略未知键也无害）
            edge["label"] = str(ed["label"])
        out_edges.append(edge)
        if len(out_edges) >= _MAX_EDGES:                 # 上限保护
            break

    data = {"nodes": out_nodes, "edges": out_edges}
    return {
        "render": {"kind": "call_graph", "data": data},
        # 只回一句 summary（与模式 A 一致：render.data 不灌回 LLM，省 token）
        "summary": f"已渲染逻辑图（{len(out_nodes)} 节点）",
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
        # 模式 B（freeform）：agent 给了 nodes → 直接构造任意逻辑/架构图（不触图后端 BFS）
        if input.get("nodes"):
            return _build_freeform_graph(input.get("nodes"), input.get("edges") or [])
        # 模式 A：校验 entity_id；既无 entity_id 又无 nodes → 返回错误信号（render=None，LLM 不会误以为成功）
        entity_id = input.get("entity_id")
        if not entity_id:
            return {"render": None, "summary": "缺少 entity_id 或 nodes，无法渲染图", "error": "need entity_id or nodes"}

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
