"""Fix-2 确定性 call_chain 注入 单测。

设计 [[召回链路缺陷诊断与修复方案]] Fix-2：synthesize 后若无 call_chain 段、但 ctx 有多跳调用边，
后端用边确定性构造 call_chain JSON 段注入（过滤框架噪声），保证流程类问题出 ReactFlow。
另修 _ctx_to_dict 漏带 call_edges_by_entry（C2 gap）。
"""
import json

from src.service.qa_engine.retriever import RetrievedContext
from src.service.qa_engine.synthesizer import (
    _build_call_chain_section_from_edges,
    _ensure_call_chain_section,
    _ctx_to_dict,
)


def test_ctx_to_dict_includes_call_edges():
    """_ctx_to_dict 必须带 call_edges_by_entry（否则 prompt 拿不到多跳边）。"""
    ctx = RetrievedContext(question="q", project_id="p")
    ctx.call_edges_by_entry = {"A::c": [("A::c", "B::c")]}
    d = _ctx_to_dict(ctx)
    assert d.get("call_edges_by_entry") == {"A::c": [("A::c", "B::c")]}


def test_ctx_to_dict_includes_callchain_node_summaries():
    """_ctx_to_dict 必须透传 callchain_node_summaries（否则 prompt 拿不到解读）。"""
    ctx = RetrievedContext(question="q", project_id="p")
    ctx.callchain_node_summaries = {"C::register#(p)": "会员注册入口"}
    d = _ctx_to_dict(ctx)
    assert d.get("callchain_node_summaries") == {"C::register#(p)": "会员注册入口"}


def test_build_call_chain_filters_noise_and_preserves_edges():
    """从 call_edges 构造 call_chain：保边、短名 label、过滤 CommonResult/IErrorCode 框架噪声。"""
    edges = {
        "OmsX::create": [
            ("OmsX::create", "OmsXService::create"),
            ("OmsX::create", "CommonResult::success#(Tdata)"),   # 噪声
            ("OmsXService::create", "OmsMapper::insert#(Oms)"),
        ],
    }
    sec = _build_call_chain_section_from_edges(edges)
    assert sec is not None
    assert sec["type"] == "call_chain"
    data = json.loads(sec["content"])
    node_ids = {n["id"] for n in data["nodes"]}
    # 业务节点保留
    assert "OmsX::create" in node_ids
    assert "OmsXService::create" in node_ids
    assert "OmsMapper::insert#(Oms)" in node_ids
    # 框架噪声被过滤
    assert not any("CommonResult" in nid for nid in node_ids)
    # 边保留（含多跳），噪声边被删
    pairs = {(e["from"], e["to"]) for e in data["edges"]}
    assert ("OmsX::create", "OmsXService::create") in pairs
    assert ("OmsXService::create", "OmsMapper::insert#(Oms)") in pairs
    assert not any("CommonResult" in e["to"] for e in data["edges"])
    # label = 短方法名（去类名/参数）
    labels = {n["label"] for n in data["nodes"]}
    assert "create" in labels and "insert" in labels


def test_build_call_chain_sets_kind_and_filters_accessor_noise():
    """节点按类名后缀推断 kind（前端据此着色+图标）；setter 等访问器噪声被过滤。"""
    edges = {
        "X::create": [
            ("com.x.controller.OmsController::create", "com.x.service.impl.OmsServiceImpl::create"),
            ("com.x.service.impl.OmsServiceImpl::create", "com.x.model.OmsOrder::setStatus#(Integer)"),  # accessor 噪声
            ("com.x.service.impl.OmsServiceImpl::create", "com.x.mapper.OmsMapper::insert#(o)"),
        ],
    }
    sec = _build_call_chain_section_from_edges(edges)
    data = json.loads(sec["content"])
    nodes = {n["id"]: n for n in data["nodes"]}
    # accessor 噪声节点被过滤（不在图里）
    assert not any("setStatus" in nid for nid in nodes)
    # kind 按类名后缀推断：Controller→controller、ServiceImpl→service、Mapper→mapper
    assert nodes["com.x.controller.OmsController::create"]["kind"] == "controller"
    assert nodes["com.x.service.impl.OmsServiceImpl::create"]["kind"] == "service"
    assert nodes["com.x.mapper.OmsMapper::insert#(o)"]["kind"] == "mapper"
    # entityId 带 method:// scheme（前端 EntityRef 据此点击跳源码；后端 resolve_first 会剥 scheme）
    assert nodes["com.x.controller.OmsController::create"]["entityId"] == "method://com.x.controller.OmsController::create"
    assert nodes["com.x.mapper.OmsMapper::insert#(o)"]["entityId"] == "method://com.x.mapper.OmsMapper::insert#(o)"


def test_short_cn_label_strips_marker_and_takes_first_phrase():
    """_short_cn_label 去掉 2b 解读开头的 [摘要] 等方括号小节标记，取首个短语作 label。"""
    from src.service.qa_engine.synthesizer import _short_cn_label
    assert _short_cn_label("[摘要] 接收注册申请 校验后落库") == "接收注册申请"
    assert _short_cn_label("订单创建入口，接收下单参数") == "订单创建入口"
    assert _short_cn_label("") == ""


def test_build_call_chain_uses_chinese_label_from_summaries():
    """确定性图（belt-and-suspenders）：节点有 2b 中文解读时 label 用解读首句（中文业务动作），
    无解读则回退方法短名——保证覆盖到的方法即使 LLM 不产 A1 图也是中文。"""
    edges = {"X::create": [("com.x.OmsController::create", "com.x.OmsServiceImpl::create")]}
    summaries = {"com.x.OmsController::create": "订单创建入口，接收下单参数，校验后委派业务层"}
    sec = _build_call_chain_section_from_edges(edges, node_summaries=summaries)
    data = json.loads(sec["content"])
    nodes = {n["id"]: n for n in data["nodes"]}
    # 有解读 → label 用中文首句（到第一个逗号/句号，截断）
    assert nodes["com.x.OmsController::create"]["label"] == "订单创建入口"
    # 无解读 → 回退方法短名
    assert nodes["com.x.OmsServiceImpl::create"]["label"] == "create"


def test_build_call_chain_label_falls_back_to_method_when_no_summaries():
    """不传 node_summaries（旧调用）→ label 仍是方法短名（向后兼容）。"""
    edges = {"X::create": [("com.x.OmsController::create", "com.x.OmsServiceImpl::create")]}
    sec = _build_call_chain_section_from_edges(edges)
    data = json.loads(sec["content"])
    labels = {n["label"] for n in data["nodes"]}
    assert labels == {"create"}


def test_build_call_chain_none_when_empty_or_all_noise():
    """无边 / 全是框架噪声 → 返回 None（不注入空图）。"""
    assert _build_call_chain_section_from_edges({}) is None
    assert _build_call_chain_section_from_edges(None) is None
    all_noise = {"A::f": [("CommonResult::success#()", "IErrorCode::getCode#()")]}
    assert _build_call_chain_section_from_edges(all_noise) is None


def test_ensure_injects_after_entry_point_when_absent():
    """无 call_chain 段 + ctx 有边 → 注入；位置在 entry_point 之后。"""
    ctx = RetrievedContext(question="q", project_id="p")
    ctx.call_edges_by_entry = {"A::create": [("A::create", "B::create")]}
    sections = [
        {"type": "overview", "content": "视角：overall-architecture"},
        {"type": "entry_point", "content": "A::create"},
        {"type": "references", "content": "..."},
    ]
    out = _ensure_call_chain_section(sections, ctx)
    types = [s["type"] for s in out]
    assert "call_chain" in types
    assert types.index("call_chain") == types.index("entry_point") + 1


def test_ensure_skips_when_call_chain_already_present():
    """LLM 已自己产出 call_chain 段 → 不重复注入。"""
    ctx = RetrievedContext(question="q", project_id="p")
    ctx.call_edges_by_entry = {"A::create": [("A::create", "B::create")]}
    sections = [{"type": "call_chain", "content": '{"nodes":[],"edges":[]}'}]
    out = _ensure_call_chain_section(sections, ctx)
    assert sum(1 for s in out if s["type"] == "call_chain") == 1


def test_ensure_noop_when_no_edges():
    """ctx 无边（如 chit-chat）→ 不注入。"""
    ctx = RetrievedContext(question="q", project_id="p")  # call_edges_by_entry 默认 {}
    sections = [{"type": "overview", "content": "..."}]
    out = _ensure_call_chain_section(sections, ctx)
    assert not any(s["type"] == "call_chain" for s in out)
