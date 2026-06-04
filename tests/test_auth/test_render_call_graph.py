"""render_call_graph 工具：从 entity_id 遍历图、复用调用图构建、产出 {render, summary}。"""
import asyncio  # 跑 async handler
import json     # 校验 render.data 是合法结构（json 序列化往返）
from src.service.qa_engine.tools.render_call_graph import build_render_call_graph_tool


class _FakeGraph:
    """假 GraphProto：用邻接表模拟 successors/predecessors。"""
    def __init__(self, succ):
        self._succ = succ                       # {node: [子节点...]}
    def successors(self, nid):
        return list(self._succ.get(nid, []))    # 下游
    def predecessors(self, nid):
        # 反查：谁的 successors 里含 nid
        return [k for k, vs in self._succ.items() if nid in vs]


def test_render_call_graph_down_builds_payload():
    # generateOrder → lockStock / hasStock；下游 2 跳
    g = _FakeGraph({
        "Svc::generateOrder#(P)": ["Svc::lockStock#()", "Svc::hasStock#()"],
        "Svc::lockStock#()": ["Mapper::updateStock#()"],
    })
    tool = build_render_call_graph_tool(g)
    out = asyncio.run(tool.handler({"entity_id": "Svc::generateOrder#(P)", "direction": "down", "depth": 2}))
    # 含 render 块 + 一句 summary
    assert out["render"]["kind"] == "call_graph"
    data = out["render"]["data"]
    assert len(data["nodes"]) >= 3                       # generateOrder/lockStock/hasStock/updateStock
    assert {"from", "to"}.issubset(data["edges"][0].keys())
    assert "调用图" in out["summary"] and "节点" in out["summary"]


def test_render_call_graph_no_edges_returns_none_render():
    g = _FakeGraph({})                                    # 无邻居
    tool = build_render_call_graph_tool(g)
    out = asyncio.run(tool.handler({"entity_id": "Svc::lonely#()"}))
    assert out["render"] is None                          # 无图不渲染
    assert "未找到" in out["summary"]


def test_render_call_graph_missing_entity_id():
    tool = build_render_call_graph_tool(_FakeGraph({}))
    out = asyncio.run(tool.handler({}))
    assert out["render"] is None
    assert out.get("error")                               # 给 LLM 错误信号


def test_render_call_graph_label_uses_summary_lookup():
    # summary_lookup 提供中文解读 → label 取中文短语
    g = _FakeGraph({"Svc::pay#()": ["Svc::notify#()"]})
    tool = build_render_call_graph_tool(g, summary_lookup=lambda nid: "发起支付宝支付 返回表单" if "pay" in nid else "")
    out = asyncio.run(tool.handler({"entity_id": "Svc::pay#()", "direction": "down", "depth": 1}))
    labels = [n["label"] for n in out["render"]["data"]["nodes"]]
    assert any("支付" in l for l in labels)               # 中文 label 生效
