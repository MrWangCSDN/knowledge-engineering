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
