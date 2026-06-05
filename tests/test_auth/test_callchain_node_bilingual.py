"""调用图节点中英双语：label=中文业务名 + method=英文 class.method（治"节点看着空洞"）。"""
import json
from src.service.qa_engine.synthesizer import _build_call_chain_section_from_edges


def test_node_has_chinese_label_and_english_method():
    """有 2b 中文解读的节点：label 取中文业务名，且单独带英文 class.method 供前端副标题展示。"""
    call_edges = {"e": [("OmsPortalOrderServiceImpl::generateOrder",
                         "OmsPortalOrderServiceImpl::lockStock")]}
    summaries = {"OmsPortalOrderServiceImpl::generateOrder": "生成订单主流程"}
    sec = _build_call_chain_section_from_edges(call_edges, node_summaries=summaries)
    assert sec is not None
    nodes = json.loads(sec["content"])["nodes"]
    n = next(x for x in nodes if x["id"] == "OmsPortalOrderServiceImpl::generateOrder")
    assert "生成订单" in n["label"]                                      # 中文业务名
    assert n["method"] == "OmsPortalOrderServiceImpl.generateOrder"    # 英文 class.method（去包名短类名 + 方法）


def test_node_without_summary_still_has_method():
    """无中文解读的节点：label 回退方法短名，method 仍给英文 class.method（不缺字段）。"""
    call_edges = {"e": [("OmsPortalOrderServiceImpl::generateOrder",
                         "OmsPortalOrderServiceImpl::lockStock")]}
    sec = _build_call_chain_section_from_edges(call_edges, node_summaries={})
    nodes = json.loads(sec["content"])["nodes"]
    n = next(x for x in nodes if x["id"] == "OmsPortalOrderServiceImpl::lockStock")
    assert n["method"] == "OmsPortalOrderServiceImpl.lockStock"
