"""接地校验 _ground_call_chain_sections 单测（[[逻辑图中文化-设计]] §4.3）。

A1 锚定式：LLM 产的 call_chain 节点 entityId 必须 ∈ 调用链真实方法集；虚构节点丢弃、
连带丢引用它的边；有效节点 < 2 → 删整段（交兜底重注入）。边只校验引用合法保留 node id（允许逻辑边）。
"""
import json

from src.service.qa_engine.retriever import RetrievedContext
from src.service.qa_engine.synthesizer import (
    _ground_call_chain_sections,
    _recalled_ids,
)


def _cc_section(nodes, edges):
    """构造一个 call_chain section dict（content 为 CallChain JSON 字符串）。"""
    return {"type": "call_chain", "title": "业务流程",
            "content": json.dumps({"nodes": nodes, "edges": edges}, ensure_ascii=False)}


def test_recalled_ids_collects_edge_endpoints():
    """_recalled_ids = call_edges_by_entry 所有边的去重端点集。"""
    ctx = RetrievedContext(question="q", project_id="p")
    ctx.call_edges_by_entry = {
        "C::reg#(p)": [("C::reg#(p)", "S::reg#(p)"), ("S::reg#(p)", "M::save#(m)")],
    }
    assert _recalled_ids(ctx) == {"C::reg#(p)", "S::reg#(p)", "M::save#(m)"}


def test_ground_keeps_real_nodes_and_drops_hallucinated():
    """节点 entityId 在召回集→保留；不在（虚构）→丢节点 + 丢引用它的边。"""
    recalled = {"C::reg#(p)", "S::reg#(p)"}
    sec = _cc_section(
        nodes=[
            {"id": "n1", "label": "注册入口", "entityId": "method://C::reg#(p)"},
            {"id": "n2", "label": "注册业务", "entityId": "method://S::reg#(p)"},
            {"id": "n3", "label": "虚构的发短信", "entityId": "method://X::sendSms#()"},  # 幻觉
        ],
        edges=[
            {"from": "n1", "to": "n2", "label": "校验通过后"},
            {"from": "n2", "to": "n3", "label": "虚构边"},  # 引用 n3 → 应删
        ],
    )
    out = _ground_call_chain_sections([sec], recalled)
    data = json.loads(out[0]["content"])
    ids = {n["id"] for n in data["nodes"]}
    assert ids == {"n1", "n2"}                         # 虚构 n3 被丢
    assert all(e["to"] != "n3" for e in data["edges"])  # 引用 n3 的边被丢
    assert {(e["from"], e["to"]) for e in data["edges"]} == {("n1", "n2")}


def test_ground_drops_whole_section_when_valid_nodes_lt_2():
    """接地后有效节点 < 2 → 整段被删（LLM 整体跑偏，交兜底）。"""
    recalled = {"C::reg#(p)"}
    sec = _cc_section(
        nodes=[
            {"id": "n1", "label": "注册入口", "entityId": "method://C::reg#(p)"},
            {"id": "n2", "label": "虚构", "entityId": "method://X::hallucinate#()"},
        ],
        edges=[{"from": "n1", "to": "n2"}],
    )
    out = _ground_call_chain_sections([sec], recalled)
    assert all(s.get("type") != "call_chain" for s in out)  # call_chain 段被删


def test_ground_tolerates_entityid_without_scheme():
    """节点 entityId 不带 scheme（裸 qn）也能匹配召回集。"""
    recalled = {"C::reg#(p)", "S::reg#(p)"}
    sec = _cc_section(
        nodes=[
            {"id": "n1", "label": "入口", "entityId": "C::reg#(p)"},       # 裸 qn
            {"id": "n2", "label": "业务", "entityId": "method://S::reg#(p)"},
        ],
        edges=[{"from": "n1", "to": "n2"}],
    )
    out = _ground_call_chain_sections([sec], recalled)
    data = json.loads(out[0]["content"])
    assert {n["id"] for n in data["nodes"]} == {"n1", "n2"}


def test_ground_noop_on_non_callchain_sections():
    """非 call_chain 段原样返回。"""
    secs = [{"type": "overview", "content": "视角：x"}]
    assert _ground_call_chain_sections(secs, {"C::reg#(p)"}) == secs


def test_ground_keeps_non_json_callchain_like_mermaid():
    """非 JSON CallChain（如 mermaid fence）→ 接地不适用，原样保留（不误删）。"""
    secs = [{"type": "call_chain", "content": "```mermaid\ngraph LR\n  A --> B\n```"}]
    out = _ground_call_chain_sections(secs, {"C::reg#(p)"})
    assert out == secs  # 原样保留


def test_ground_noop_when_recalled_empty():
    """recalled 集为空（无调用边）→ 无从接地，整体原样返回（call_chain 不被误删）。"""
    sec = _cc_section(
        nodes=[{"id": "n1", "label": "x", "entityId": "method://Z::z#()"}],
        edges=[],
    )
    out = _ground_call_chain_sections([sec], set())
    assert out == [sec]
