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


def test_render_call_graph_falls_back_to_opposite_direction():
    # 候选节点上下游不对称（典型：Dao 只有上游）：请求 down 无下游 → 自动回退 up → 仍出图
    g = _FakeGraph({"Caller::m#()": ["Leaf::x#()"]})   # Leaf 有 predecessor(Caller)、无 successor
    tool = build_render_call_graph_tool(g)
    out = asyncio.run(tool.handler({"entity_id": "Leaf::x#()", "direction": "down", "depth": 2}))
    assert out["render"] is not None                        # down 空 → 回退 up → 出图
    assert len(out["render"]["data"]["nodes"]) >= 2
    assert "上游" in out["summary"]                          # summary 反映实际命中方向


def test_render_call_graph_registered_in_factory():
    """工具工厂 build_default_registry 应注册 render_call_graph（与 ke_callees 等并列）。"""
    from src.service.qa_engine.tools import build_default_registry

    class _G:                                              # 最小 GraphProto stub（构造工具不触发遍历）
        def successors(self, n): return []
        def predecessors(self, n): return []

    reg = build_default_registry(graph=_G(), interpretation_store=object(), project_id="p")
    names = [t.name for t in reg.list_tools()]
    assert "render_call_graph" in names


def test_render_call_graph_factory_wires_summary_lookup_from_interpretation_store():
    """build_default_registry 应把 interpretation_store.get_by_entity 接到 summary_lookup → 节点 label 中文化。

    历史：tools/__init__.py 注释 "summary_lookup 暂留 None" → 调用图节点全英文方法名。
    本测试守住"接通"不变量：interpretation_store 有 2b 解读 → 节点 label 中文化。
    """
    from src.service.qa_engine.tools import build_default_registry

    g = _FakeGraph({"Svc::pay#()": ["Svc::notify#()"]})

    class _Store:
        """假 interpretation_store：get_by_entity 返 2b 解读 dict。"""
        def get_by_entity(self, entity_id, level=None):
            # 模拟 composite/interpretation store 返回结构（含 interpretation_text 字段）
            if "pay" in entity_id:
                return {"interpretation_text": "发起支付宝支付 返回支付表单"}
            return None

    reg = build_default_registry(graph=g, interpretation_store=_Store(), project_id="p")
    tool = reg.get("render_call_graph")                  # 取注册的工具实例
    out = asyncio.run(tool.handler({"entity_id": "Svc::pay#()", "direction": "down", "depth": 1}))
    labels = [n["label"] for n in out["render"]["data"]["nodes"]]
    # 期望 pay 节点 label 含"支付"中文；notify 节点 _Store 返 None → fallback 方法名 'notify'
    assert any("支付" in l for l in labels), f"labels={labels}（中文 label 未生效）"


def test_render_call_graph_factory_summary_lookup_fail_soft():
    """interpretation_store.get_by_entity 抛异常 → summary_lookup 返 "" → 不传染、节点回退英文。

    fail-soft 契约：tenant 不存在 / 网络抖动 / 字段缺失等异常都不应让图渲染失败。
    """
    from src.service.qa_engine.tools import build_default_registry

    g = _FakeGraph({"Svc::pay#()": ["Svc::notify#()"]})

    class _BrokenStore:
        def get_by_entity(self, entity_id, level=None):
            raise RuntimeError("weaviate down")           # 模拟解读库挂

    reg = build_default_registry(graph=g, interpretation_store=_BrokenStore(), project_id="p")
    tool = reg.get("render_call_graph")
    out = asyncio.run(tool.handler({"entity_id": "Svc::pay#()", "direction": "down", "depth": 1}))
    # 图照样渲染（无中文，但不崩）
    assert out["render"] is not None
    assert len(out["render"]["data"]["nodes"]) >= 2
