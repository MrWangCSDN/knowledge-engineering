"""快赢 A+B 接线测试：召回 query 预处理 + top_k env + 渲染上限。

设计：[[召回链路缺陷诊断与修复方案]] 快赢 A+B。
"""
from unittest.mock import MagicMock

import pytest

from src.service.qa_engine.retriever import QARetriever
from src.service.qa_engine.sse_emitter import _recall_top_k, _RECALL_TOP_K_DEFAULT
from src.service.qa_engine.prompts import build_user_prompt, TOP_CANDIDATES_FOR_PROMPT


# ─── A 接线：召回用提纯 query；门控/ctx.question 仍用原始问题 ──────────────────

@pytest.mark.asyncio
async def test_retriever_uses_cleaned_query_keeps_original_question():
    """retrieve 应把"提纯后的 query"传给向量检索（剥掉渲染指令），
    但 ctx.question 仍是用户**原始**问题（保留意图供作答）。
    """
    captured = {}

    def fake_search(*, text, project_id, limit):
        # 记录召回实际用的查询文本 + limit
        captured["text"] = text
        captured["limit"] = limit
        return []  # 空召回 → 走闲聊分支；不影响本测试断言

    store = MagicMock()
    store.search_method_hits_by_text.side_effect = fake_search
    retriever = QARetriever(interpretation_store=store, graph=MagicMock())

    q = "下单流程是怎么实现的，用流程图展示"
    ctx = await retriever.retrieve(question=q, project_id="mall-swarm", top_k=15)

    # 召回用的是提纯后的 query（渲染指令被剥离，核心语义保留）
    assert "流程图" not in captured["text"]
    assert "展示" not in captured["text"]
    assert "下单流程" in captured["text"]
    # limit 即传入的 top_k
    assert captured["limit"] == 15
    # ctx.question 仍是原始问题
    assert ctx.question == q


# ─── B1：top_k 从 env 读，默认 15 ────────────────────────────────────────────

def test_recall_top_k_default_and_env_override(monkeypatch):
    """KE_RECALL_TOP_K：缺失→默认 15；合法数字→采用；非法/非正→回落默认。"""
    monkeypatch.delenv("KE_RECALL_TOP_K", raising=False)
    assert _recall_top_k() == 15
    assert _RECALL_TOP_K_DEFAULT == 15

    monkeypatch.setenv("KE_RECALL_TOP_K", "20")
    assert _recall_top_k() == 20

    monkeypatch.setenv("KE_RECALL_TOP_K", "abc")   # 非数字 → 默认
    assert _recall_top_k() == 15

    monkeypatch.setenv("KE_RECALL_TOP_K", "0")     # 非正 → 默认
    assert _recall_top_k() == 15


# ─── B2：build_user_prompt 渲染上限抬到 10（不再卡 5）────────────────────────

def test_build_user_prompt_renders_up_to_ten_candidates():
    """给 12 个候选，前 10 个应被渲染进 prompt（旧实现只渲染 5 个）。"""
    assert TOP_CANDIDATES_FOR_PROMPT == 10
    cands = [
        {"entity_id": f"C{i}::m#()", "level": "method", "summary_text": f"说明{i}"}
        for i in range(12)
    ]
    prompt = build_user_prompt("问题", {"entry_candidates": cands})

    # 前 10 个（索引 0..9）应在 prompt 中
    for i in range(10):
        assert f"C{i}::m#()" in prompt
    # 第 11、12 个（索引 10/11）超过上限，不渲染
    assert "C10::m#()" not in prompt
    assert "C11::m#()" not in prompt


# ─── C2：调用链路多跳展开（保边）─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retriever_call_edges_preserve_multi_hop_edges():
    """C2：call_edges_by_entry 应保留多跳 from→to 边（深度 2），而非扁平节点。"""
    store = MagicMock()
    store.search_method_hits_by_text.return_value = [
        {"entity_id": "A::gen#()", "summary_text": "下单入口", "level": "method", "score": 0.9}
    ]

    # mock 图：A→B、A→C（1 跳）；B→D（2 跳）
    chain = {"A::gen#()": ["B::svc#()", "C::svc#()"], "B::svc#()": ["D::dao#()"]}

    def successors(node, rel_type=None):
        if rel_type == "accesses_table":
            return []          # 无表访问边
        return chain.get(node, [])

    graph = MagicMock()
    graph.successors.side_effect = successors
    graph.predecessors.return_value = []
    graph.module_of.return_value = None

    # recall_threshold=0.0 → top1(0.9) 过线走 architecture，触发链路展开
    retriever = QARetriever(interpretation_store=store, graph=graph, recall_threshold=0.0)
    ctx = await retriever.retrieve(question="下单流程", project_id="p", top_k=5)

    edges = ctx.call_edges_by_entry.get("A::gen#()")
    assert edges is not None
    # 1 跳边
    assert ("A::gen#()", "B::svc#()") in edges
    assert ("A::gen#()", "C::svc#()") in edges
    # 2 跳边（保边的关键：D 是 B 的 callee，不能记成 A 的直接 callee）
    assert ("B::svc#()", "D::dao#()") in edges


def test_build_user_prompt_renders_call_edges_block():
    """C2：call_edges_by_entry 渲染成"调用链路（…from→to）"块，含多跳边。"""
    ctx = {
        "entry_candidates": [{"entity_id": "A::gen#()", "level": "method", "summary_text": "下单入口"}],
        "call_edges_by_entry": {
            "A::gen#()": [("A::gen#()", "B::svc#()"), ("B::svc#()", "D::dao#()")]
        },
    }
    p = build_user_prompt("下单流程", ctx)
    assert "调用链路" in p
    assert "A::gen#()  →  B::svc#()" in p
    assert "B::svc#()  →  D::dao#()" in p   # 2 跳边也渲染出来
