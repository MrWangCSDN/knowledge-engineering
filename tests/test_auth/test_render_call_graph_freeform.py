"""render_call_graph 模式 B（freeform）：agent 直接给 nodes/edges → 渲染任意 reactflow 图
（业务逻辑/架构图，不经图后端 BFS）。设计 [[业务问答-reactflow御用画图工具-设计]] §四②。

输出必须与模式 A（_build_call_chain_section_from_edges）同构：
  node = {id, label(中), method(英), kind, [entityId]}；edge = {from, to}（非 source/target）。
前端 CallChainFlow / MethodNode 零改动即可吃。
"""
import asyncio

from src.service.qa_engine.tools.render_call_graph import build_render_call_graph_tool


class _StubGraph:
    """模式 B 不该触图后端：一旦被调用即断言失败，证明 freeform 是纯构造、不 BFS。"""

    def successors(self, n):
        raise AssertionError("freeform 模式不应触图后端 successors")

    def predecessors(self, n):
        raise AssertionError("freeform 模式不应触图后端 predecessors")


def _run(coro):
    """跑一个 async handler 拿结果：asyncio.run 每次起新事件循环，避免与其它 async 测试共享已关闭的 loop。"""
    return asyncio.run(coro)


def test_freeform_nodes_edges_renders():
    """给 nodes/edges（input 用 code/source/target）→ 输出同构 render data（method/from/to）。"""
    tool = build_render_call_graph_tool(_StubGraph())
    out = _run(tool.handler({
        "nodes": [
            {"id": "a", "label": "下单", "code": "OrderController.submit", "kind": "controller"},
            {"id": "b", "label": "扣库存", "code": "StockService.deduct", "kind": "service"},
        ],
        "edges": [{"source": "a", "target": "b", "label": "调用"}],
    }))
    assert out["render"]["kind"] == "call_graph"
    data = out["render"]["data"]
    assert len(data["nodes"]) == 2
    assert len(data["edges"]) == 1
    # 节点双语字段：label=中文业务名，method=英文代码标识
    n0 = next(n for n in data["nodes"] if n["id"] == "a")
    assert n0["label"] == "下单"
    assert n0["method"] == "OrderController.submit"
    assert n0["kind"] == "controller"
    # 边归一化为 from/to（与模式 A 同构，前端吃 from/to 不是 source/target）
    e0 = data["edges"][0]
    assert e0["from"] == "a" and e0["to"] == "b"


def test_freeform_drops_dangling_edges():
    """边引用了不存在的节点 → 丢弃悬挂边（防前端渲染崩）。"""
    tool = build_render_call_graph_tool(_StubGraph())
    out = _run(tool.handler({
        "nodes": [{"id": "a", "label": "A"}],
        "edges": [{"from": "a", "to": "ghost"}],   # ghost 不在节点集
    }))
    assert out["render"]["kind"] == "call_graph"
    assert out["render"]["data"]["edges"] == []     # 悬挂边被丢


def test_freeform_empty_nodes_is_error():
    """nodes 空 → error 信号（render=None），agent 改文字、不崩。"""
    tool = build_render_call_graph_tool(_StubGraph())
    out = _run(tool.handler({"nodes": [], "edges": []}))
    assert out["render"] is None
    assert "error" in out


def test_entity_and_nodes_both_missing_is_error():
    """既无 entity_id 又无 nodes → error。"""
    tool = build_render_call_graph_tool(_StubGraph())
    out = _run(tool.handler({}))
    assert out["render"] is None
    assert "error" in out


# ── mode B 边核验（2026-06-08 加）─────────────────────────────────────────
# 用户实测：LLM 在异步边界（MQ @Bean / Spring 配置）编伪 calls 边。修：两端点都
# 能映射回 CodeGraph qualified_name 时校验，CodeGraph 无该边 → drop；端点是抽象
# 概念时不校验，避免杀真业务流程图。


def test_freeform_drops_unsupported_edges_in_codegraph():
    """两端都是真实方法 entity_id 但 CodeGraph 无 calls/refs 边 → drop（治 LLM 伪边）。"""

    # 假 graph：仅 generateOrder→sendDelay 边真实；MQ 配置不在 graph 里
    class _Graph:
        def successors(self, entity_id):
            if "generateOrder" in entity_id:
                return ["Cls::sendDelay#()"]               # 真实下游
            return []                                       # 其他无下游

        def predecessors(self, entity_id):
            return []                                       # 简化：仅靠 successors 命中即可

    tool = build_render_call_graph_tool(_Graph())
    out = _run(tool.handler({
        "nodes": [
            # id 含 "::"：被识别为真实 CodeGraph 方法 → 触发校验
            {"id": "Cls::generateOrder", "label": "下单", "entityId": "method://Cls::generateOrder"},
            {"id": "Cls::sendDelay", "label": "发延迟消息", "entityId": "method://Cls::sendDelay"},
            {"id": "QConfig::orderTtlQueue", "label": "队列配置", "entityId": "method://QConfig::orderTtlQueue"},
        ],
        "edges": [
            {"source": "Cls::generateOrder", "target": "Cls::sendDelay"},          # ✅ 真
            {"source": "Cls::sendDelay", "target": "QConfig::orderTtlQueue"},      # ❌ 伪（MQ 边界）
        ],
    }))
    data = out["render"]["data"]
    edge_set = {(e["from"], e["to"]) for e in data["edges"]}
    assert ("Cls::generateOrder", "Cls::sendDelay") in edge_set        # 真边保留
    assert ("Cls::sendDelay", "QConfig::orderTtlQueue") not in edge_set  # 伪边丢
    assert "已过滤" in out["summary"] and "1" in out["summary"]       # summary 提示丢了 1 条


def test_freeform_keeps_edges_with_abstract_endpoints():
    """端点 id 不像 qualified_name（抽象概念节点）→ 不校验保留（不杀业务流程图）。"""

    class _Graph:                                          # 即便能查也不会被调（id 无 "::"）
        def successors(self, entity_id): return []
        def predecessors(self, entity_id): return []

    tool = build_render_call_graph_tool(_Graph())
    out = _run(tool.handler({
        "nodes": [
            {"id": "user", "label": "用户"},               # 无 "::" → 抽象概念
            {"id": "queue", "label": "消息队列"},          # 无 "::" → 抽象概念
        ],
        "edges": [{"source": "user", "target": "queue", "label": "下单"}],
    }))
    assert len(out["render"]["data"]["edges"]) == 1        # 保留
    assert "已过滤" not in out["summary"]                  # 无过滤提示


def test_freeform_keeps_when_graph_throws():
    """图后端挂了（successors/predecessors 抛错）→ 边保留（fail-safe，不冤枉）。"""

    class _BrokenGraph:
        def successors(self, entity_id): raise RuntimeError("graph backend down")
        def predecessors(self, entity_id): raise RuntimeError("graph backend down")

    tool = build_render_call_graph_tool(_BrokenGraph())
    out = _run(tool.handler({
        "nodes": [{"id": "Cls::a"}, {"id": "Cls::b"}],     # 看似真实方法
        "edges": [{"source": "Cls::a", "target": "Cls::b"}],
    }))
    # 图后端抛错时不冤枉：边保留
    assert len(out["render"]["data"]["edges"]) == 1
    assert "已过滤" not in out["summary"]


def test_freeform_semantic_labeled_edges_pass_validation():
    """边 label 含语义关键词（异步/MQ/触发/监听/配置/路由/事件 等）→ 跳过 calls 校验保留。

    2026-06-08 实测决策：逻辑图本质是业务逻辑图，MQ / @Scheduled / @Listener / 配置
    这些异步/语义关系也是业务的一部分；不该一刀切禁画。修：边 label 明确标注语义
    关系时 → 跳过 CodeGraph calls 校验直接保留；纯 calls 边仍严格校验。
    """

    class _Graph:
        # 任何 source 在 CodeGraph 都无 calls 下游（模拟 MQ @Bean 无 calls 边场景）
        def successors(self, qn):
            return []
        def predecessors(self, qn):
            return []

    tool = build_render_call_graph_tool(_Graph())
    out = _run(tool.handler({
        "nodes": [
            {"id": "a", "label": "发送", "method": "Sender::send"},
            {"id": "b", "label": "队列", "method": "RabbitMqConfig::orderTtlQueue"},
            {"id": "c", "label": "消费者", "method": "Listener::handle"},
        ],
        "edges": [
            # label 含 "MQ" → 语义边，保留（即使 CodeGraph 无该 calls 边）
            {"source": "a", "target": "b", "label": "MQ 路由"},
            # label 含 "异步触发" → 语义边，保留
            {"source": "b", "target": "c", "label": "异步触发"},
            # label "调用" → 纯 calls 边，CodeGraph 无支撑 → drop
            {"source": "a", "target": "c", "label": "调用"},
        ],
    }))
    data = out["render"]["data"]
    edge_set = {(e["from"], e["to"]) for e in data["edges"]}
    # 两条语义边保留
    assert ("a", "b") in edge_set
    assert ("b", "c") in edge_set
    # 纯 calls 边 drop
    assert ("a", "c") not in edge_set
    # summary 提示丢了 1 条
    assert "已过滤" in out["summary"] and "1" in out["summary"]


def test_freeform_normalizes_dot_to_double_colon():
    """漏洞修补 2/2：agent 按 prompt 用 'Class.method' 形态时归一为 'Class::method' 后查 CodeGraph。

    2026-06-08 mall-swarm 实测：agent 输出 method 字段用 . 分隔（如
    'OrderTimeOutCancelTask.cancelTimeOutOrder'），原 _node_qn 看 :: 不命中 → 当抽象节点
    跳过校验。修：识别 'Class.method' 形态，归一为 CodeGraph 的 'Class::method' qn。
    """

    class _Graph:
        # CancelOrderSender.sendMessage → orderTtlQueue 在 CodeGraph 无 calls 边（@Bean 不被调）
        def successors(self, qn):
            return []
        def predecessors(self, qn):
            return []

    tool = build_render_call_graph_tool(_Graph())
    out = _run(tool.handler({
        "nodes": [
            # 用 . 分隔的实测格式（agent 按 prompt 走"code(英文 类.方法)"约定）
            {"id": "1", "label": "发送", "method": "CancelOrderSender.sendMessage", "kind": "service"},
            {"id": "2", "label": "队列", "method": "RabbitMqConfig.orderTtlQueue", "kind": "config"},
        ],
        "edges": [
            # label "调用" 不含语义关键词 → 走纯 calls 严格校验 → 因 . 形态归一为 :: 后被识别
            # 为真实方法 + CodeGraph 无 calls → drop
            {"source": "1", "target": "2", "label": "调用"},
        ],
    }))
    data = out["render"]["data"]
    assert len(data["edges"]) == 0
    assert "已过滤" in out["summary"]


def test_freeform_extracts_qn_from_method_or_code_field():
    """漏洞修补：agent 用中文 id + method/code 字段藏 qn 时也能校验（2026-06-08 修）。

    2026-06-08 mall-swarm 实测案例：agent 输出节点 {id: "MQ配置", method:
    "RabbitMqConfig::orderTtlQueue"} 形态绕过了 _node_qn 的 entityId/id 提取，导致
    跨 MQ 边界的假 calls 边漏过 freeform 校验。修：也从 method / code 字段抽 qn。
    """

    class _Graph:
        # sendDelay → orderTtlQueue 在 CodeGraph 里无 calls 边（@Bean 不被调）
        def successors(self, qn):
            return []                                       # 任何 source 都无下游
        def predecessors(self, qn):
            return []

    tool = build_render_call_graph_tool(_Graph())
    out = _run(tool.handler({
        "nodes": [
            # mall-swarm 实测格式：id 是中文标签，method 是 qualified_name，entityId 不设
            {"id": "订单创建时", "label": "订单创建时", "method": "OmsPortalOrderServiceImpl::sendDelayMessageCancelOrder", "kind": "service"},
            {"id": "MQ配置",     "label": "MQ延迟消息", "method": "RabbitMqConfig::orderTtlQueue",                          "kind": "mq"},
        ],
        "edges": [
            # 该边在 CodeGraph 里不存在 → 应被识别 + 丢弃
            {"source": "订单创建时", "target": "MQ配置"},
        ],
    }))
    data = out["render"]["data"]
    # 节点保留（节点不删），但边被丢
    assert len(data["nodes"]) == 2
    assert len(data["edges"]) == 0
    assert "已过滤" in out["summary"]
