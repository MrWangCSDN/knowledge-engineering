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

# logging：mode B 边核验丢弃的伪边走 INFO 留痕（同 ke_callees 风格）
import logging
_LOG = logging.getLogger(__name__)

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


def _normalize_to_qn(s: str) -> str:
    """归一'Class.method' / 'Class::method' / 'method://Class::method#(p)' → 'Class::method'。

    设计：agent 按 prompt §画图约定的"code(英文 类.方法)"约定输出时用 . 分隔；
    CodeGraph 的 qualified_name 用 :: 分隔。本函数做两侧归一让 graph.successors 查得到。

    规则：
      1. 剥 'method://'/'class://' 等 scheme（split('://')[-1]）
      2. 剥 '#' 后的签名
      3. 已含 '::' → 直接返
      4. 含 '.' → 最右一个 . 替换为 ::（短类名+短方法形态）
      5. 无 . 无 :: → 原样返

    例：'OrderTimeOutCancelTask.cancelTimeOutOrder' → 'OrderTimeOutCancelTask::cancelTimeOutOrder'
       'method://Cls::m#(Long)' → 'Cls::m'
       'Cls::m' → 'Cls::m'
       'user' → 'user'
    """
    # 1. 剥 scheme
    s = s.split("://", 1)[-1]
    # 2. 剥签名
    s = s.split("#", 1)[0]
    # 3. 已含 :: → 不动
    if "::" in s:
        return s
    # 4. 含 . → 把最右一个 . 替换为 ::（短类名形态：'Class.method' → 'Class::method'）
    if "." in s:
        idx = s.rfind(".")
        return s[:idx] + "::" + s[idx + 1:]
    # 5. 无任何分隔符 → 原样返（抽象概念节点会走调用方的 None 判断）
    return s


def _node_qn(node: dict[str, Any]) -> Optional[str]:
    """从 mode B 节点 dict 抽 CodeGraph qualified_name；非真实方法节点返 None。

    优先级（依次尝试）：
      1. entityId（'method://Cls::m#(p)'）→ 剥 scheme + 签名 + 归一
      2. method 字段（_build_freeform_graph 输出格式）→ 归一
      3. code 字段（agent 输入字段）→ 归一
      4. id 字段 → 归一
      5. 都不像方法（无 :: 也无 .）→ None（抽象概念节点）

    判定"真实方法节点"：归一后含 :: 即视为真。Class.method 形态会被归一为 Class::method。
    抽象概念（如 "user" / "queue"）归一后仍无 :: → 返 None 让调用方跳过校验。

    2026-06-08 两次修补：
      ① 加 method/code 字段提取（首次实测 agent 用中文 id + method 漏过）
      ② 加 . → :: 归一（二次实测 agent 按 prompt "类.方法" 约定，原 :: 检查不命中）
    """
    # 依次尝试 entityId / method / code / id 四字段
    for field_name in ("entityId", "method", "code", "id"):
        v = node.get(field_name)
        if not v:
            continue
        qn = _normalize_to_qn(str(v))
        # 真实方法节点判定：归一后含 :: 即可（覆盖 Class::method + Class.method 两种来源）
        if "::" in qn:
            return qn
    return None


# label 含以下任一关键词 → 视为"语义边"（异步 / 配置 / 触发 / 事件 / 监听 / 路由），
# 跳过 CodeGraph calls 校验保留（用户决策 2026-06-08：逻辑图本质是业务逻辑图，
# 不该把 MQ / @Bean / AOP / @Scheduled 这些静态分析抓不到的真实关系一刀切禁画）。
# 关键词清单刻意短而具体——只覆盖"明确表达非直接调用"的语义；纯 "调用"/"实现" 等仍走严格校验。
_SEMANTIC_EDGE_KEYWORDS = (
    "异步", "MQ", "消息", "触发", "监听", "配置", "注入", "路由",
    "事件", "订阅", "发布", "广播", "回调", "调度", "定时",
    "async", "queue", "message", "listener", "trigger", "event",
)


def _is_semantic_edge(edge: dict[str, Any]) -> bool:
    """判断边是否是"语义边"——label 含上方 _SEMANTIC_EDGE_KEYWORDS 任一关键词。

    语义边表达：异步触发 / MQ 路由 / 事件监听 / 配置注入 / 定时触发 / @RabbitListener 等
    CodeGraph 静态分析抓不到但业务逻辑确实存在的关系。这种边跳过 calls 校验保留。
    """
    label = str(edge.get("label") or "")
    if not label:
        return False                                      # 无 label → 当纯 calls 边走严格校验
    # 任一关键词出现即判语义边（用 in 模糊匹配，覆盖 "MQ路由" / "异步触发" 等组合）
    return any(kw in label for kw in _SEMANTIC_EDGE_KEYWORDS)


def _filter_unsupported_edges(
    graph: Any, out_nodes: list[dict[str, Any]], out_edges: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    """mode B 边核验：两端都映射到 CodeGraph qualified_name 时检查关系是否真实，无 → drop。

    设计动机（2026-06-08，用户实测）：LLM 在 freeform 模式下画"异步触发 / Spring @Bean /
    AOP"等代码静态分析抓不到的边时会编 calls 边。例如截图中 `CancelOrderSender.sendMessage
    → RabbitMqConfig.orderTtlQueue`（@Bean 不会被调）和 `RabbitMqConfig.orderTtlQueue →
    cancelTimeOutOrder`（@Bean 不会调 Service）。本核验把"两端都是真实方法 + CodeGraph 无边"
    判为伪边丢弃，但保留**端点是抽象概念**（如 "queue" "用户"）的边——不杀真业务流程图。

    例外（2026-06-08 用户决策）：边 label 含 _SEMANTIC_EDGE_KEYWORDS（异步/MQ/触发/监听/
    配置/路由/事件 等）→ 视为"语义边"跳过 calls 校验保留，让 LLM 能完整画异步业务流程。

    fail-soft：graph 后端抛错（stub / 异常 / 没连）一律保留边，不冤枉。

    :returns: (kept_edges, dropped_count) — 保留的边列表 + 被丢的边数
    """
    # 1) 节点 id → qualified_name 映射（只对真实方法节点有值，抽象节点为 None）
    qn_by_id = {n["id"]: _node_qn(n) for n in out_nodes}
    kept: list[dict[str, Any]] = []
    dropped = 0
    for edge in out_edges:
        # 语义边（label 含 异步 / MQ / 触发 / 监听 / 配置 / 路由 / 事件 等关键词）
        # → 跳过 calls 校验保留：让业务逻辑图完整画异步桥接、配置注入、事件触发等关系
        if _is_semantic_edge(edge):
            kept.append(edge)
            continue
        src_qn = qn_by_id.get(edge["from"])
        tgt_qn = qn_by_id.get(edge["to"])
        # 任一端是抽象节点（无 qn 映射）→ 不校验，保留（LLM 画业务流程图常用抽象节点）
        if not src_qn or not tgt_qn:
            kept.append(edge)
            continue
        # 两端都看似真实方法 + 非语义边 → 询问 graph 后端是否有真实关系
        try:
            # successors/predecessors 返回 list[durable_key]（可能含 # 签名）
            succs = graph.successors(src_qn) or []
            preds = graph.predecessors(tgt_qn) or []
        except Exception:
            # graph 后端挂 / stub 抛错 → 静默保留（fail-safe，不冤枉边）
            kept.append(edge)
            continue
        # 剥签名 / scheme 取 qn 集合，做存在性比较
        def _strip(k: str) -> str:
            return k.split("://", 1)[-1].split("#", 1)[0]
        succ_qns = {_strip(s) for s in succs}
        pred_qns = {_strip(p) for p in preds}
        # 两个方向任一命中即视为"CodeGraph 有支撑"
        if tgt_qn in succ_qns or src_qn in pred_qns:
            kept.append(edge)
        else:
            # CodeGraph 无该关系 → 判伪边丢弃
            _LOG.info(
                "[render_call_graph] mode B 丢弃 CodeGraph 无支撑的边: %s → %s",
                src_qn, tgt_qn,
            )
            dropped += 1
    return kept, dropped


def _build_freeform_graph(
    nodes_in: Any, edges_in: Any, graph: Any = None
) -> dict[str, Any]:
    """模式 B：用 agent 给的 nodes/edges 直接构造 reactflow 图（业务逻辑/架构图，不触图后端 BFS）。

    边核验（2026-06-08 加）：两端都是真实方法 entity_id 但 CodeGraph 无 calls/refs/implements
    边的视为 LLM 伪边丢弃；端点为抽象概念时保留。graph=None / 后端异常一律保留。

    输出与模式 A（``_build_call_chain_section_from_edges``）**同构**，前端零改动即可吃：
      - node = ``{id, label(中文业务名), method(英文代码标识), kind, [entityId]}``
      - edge = ``{from, to, [label]}``（统一成 from/to，非 source/target）

    :param nodes_in: agent 给的节点列表，每项 ``{id, label, code?, kind?, entityId?}``
    :param edges_in: agent 给的边列表，每项 ``{source/from, target/to, label?}``
    :param graph: 图后端（GraphProto），用于边核验；None 时跳过校验
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

    # ── 边核验（2026-06-08）：丢弃 CodeGraph 无支撑的伪边 ──────────────────
    # 仅在传入 graph 时启用；测试可传 None 跳过核验。
    dropped_count = 0
    if graph is not None:
        out_edges, dropped_count = _filter_unsupported_edges(graph, out_nodes, out_edges)

    data = {"nodes": out_nodes, "edges": out_edges}
    # summary：节点数 + 被过滤的伪边数（让 LLM 知道，必要时改文字补足说明）
    summary = f"已渲染逻辑图（{len(out_nodes)} 节点）"
    if dropped_count > 0:
        summary += (
            f"，已过滤 {dropped_count} 条 CodeGraph 无支撑的边"
            "（异步触发 / Spring @Bean / AOP 等静态分析抓不到的关系）"
        )
    return {
        "render": {"kind": "call_graph", "data": data},
        # 只回一句 summary（与模式 A 一致：render.data 不灌回 LLM，省 token）
        "summary": summary,
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
        # 但传 graph 进去做"边核验"——丢弃 CodeGraph 无支撑的伪 calls 边（治 LLM 异步边界编边）
        if input.get("nodes"):
            return _build_freeform_graph(
                input.get("nodes"), input.get("edges") or [], graph=graph,
            )
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
            "渲染节点-边图（ReactFlow，唯一画图出口）。两种用法二选一："
            "① 代码调用图——传 entity_id（真实方法）+ direction(down下游/up上游)，自动 BFS 出调用关系；"
            "② 任意业务逻辑/流程/架构图——传 nodes(每项{id,label中文,code英文,kind})+edges(每项{source,target,label})，由你构思。"
            "图内联展示给用户，你只需在文字里说'见下方调用图'，不要复述节点；任何图都用我、不要手画 mermaid/reactflow。"
        ),
        input_schema=_SCHEMA,
        handler=handler,
    )
