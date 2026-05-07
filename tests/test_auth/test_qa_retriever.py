"""验证 QARetriever：业务概念 → 候选实体 + 调用链上下文。

mock 掉 business_store 和 graph，不依赖真实 Weaviate / Neo4j。
"""
from unittest.mock import MagicMock

import pytest

from src.service.qa_engine.retriever import QARetriever, RetrievedContext


# ───────── fixtures ─────────

@pytest.fixture
def mock_business_store():
    s = MagicMock()
    # 默认返回 2 个候选
    s.search_method_hits_by_text.return_value = [
        {"entity_id": "method://com.bank.openAccount", "summary_text": "存款开户主入口", "level": "api"},
        {"entity_id": "method://com.bank.kycCheck",     "summary_text": "KYC 实名认证",       "level": "method"},
    ]
    return s


@pytest.fixture
def mock_graph():
    g = MagicMock()
    g.successors.return_value = ["method://kycCheck", "method://riskApprove", "method://createAccount"]
    g.predecessors.return_value = ["controller://AccountController.openAccount"]
    return g


# ───────── 基础检索 ─────────

@pytest.mark.asyncio
async def test_retrieve_returns_context_object(mock_business_store, mock_graph):
    r = QARetriever(business_store=mock_business_store, graph=mock_graph)
    ctx = await r.retrieve(question="存款开户的设计逻辑", project_id="deposit", top_k=5)
    assert isinstance(ctx, RetrievedContext)
    assert ctx.question == "存款开户的设计逻辑"
    assert ctx.project_id == "deposit"


@pytest.mark.asyncio
async def test_retrieve_passes_project_id_filter(mock_business_store, mock_graph):
    """retriever 必须把 project_id 透传给 business_store（多工程隔离的关键）。"""
    r = QARetriever(business_store=mock_business_store, graph=mock_graph)
    await r.retrieve(question="x", project_id="deposit", top_k=5)
    # business_store 被调用时应该带 project_id
    call_kwargs = mock_business_store.search_method_hits_by_text.call_args.kwargs
    assert call_kwargs.get("project_id") == "deposit"


@pytest.mark.asyncio
async def test_retrieve_top_k_passed(mock_business_store, mock_graph):
    r = QARetriever(business_store=mock_business_store, graph=mock_graph)
    await r.retrieve(question="x", project_id="p", top_k=10)
    call_kwargs = mock_business_store.search_method_hits_by_text.call_args.kwargs
    assert call_kwargs.get("limit") == 10


@pytest.mark.asyncio
async def test_retrieve_extracts_callees_for_top_candidates(mock_business_store, mock_graph):
    r = QARetriever(business_store=mock_business_store, graph=mock_graph)
    ctx = await r.retrieve(question="x", project_id="p", top_k=5)
    # 至少 top 1 候选要有 callees
    assert "method://com.bank.openAccount" in ctx.callees_by_entry
    assert len(ctx.callees_by_entry["method://com.bank.openAccount"]) > 0


@pytest.mark.asyncio
async def test_retrieve_only_expand_top_3_to_save_cost(mock_graph):
    """成本控制：只对 top 3 候选取调用链，不是全部。"""
    bs = MagicMock()
    # 5 个候选
    bs.search_method_hits_by_text.return_value = [
        {"entity_id": f"method://m{i}", "summary_text": "x", "level": "method"}
        for i in range(5)
    ]
    r = QARetriever(business_store=bs, graph=mock_graph)
    ctx = await r.retrieve(question="x", project_id="p", top_k=10)
    # 5 个候选，但只展开了 top 3
    assert len(ctx.callees_by_entry) <= 3


# ───────── 边界情况 ─────────

@pytest.mark.asyncio
async def test_retrieve_no_candidates_returns_empty_context():
    """检索不到候选时不抛错，返回空 context。"""
    bs = MagicMock()
    bs.search_method_hits_by_text.return_value = []
    g = MagicMock()
    r = QARetriever(business_store=bs, graph=g)
    ctx = await r.retrieve(question="不存在的功能", project_id="p", top_k=5)
    assert ctx.entry_candidates == []
    assert ctx.callees_by_entry == {}


@pytest.mark.asyncio
async def test_retrieve_graph_failure_does_not_crash(mock_business_store):
    """图查询出错（如节点不存在）时不能整个流程崩，应该静默跳过。"""
    g = MagicMock()
    g.successors.side_effect = Exception("node not found")
    g.predecessors.return_value = []
    r = QARetriever(business_store=mock_business_store, graph=g)
    ctx = await r.retrieve(question="x", project_id="p", top_k=5)
    # 候选还在
    assert len(ctx.entry_candidates) == 2
    # callees 取不到，对应条目可以缺省或为空列表，不能让整个流程挂
    # （实现里要 try/except）
