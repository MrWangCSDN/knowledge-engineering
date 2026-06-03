"""验证 QARetriever：业务概念 → 候选实体 + 调用链上下文。

mock 掉 interpretation_store 和 graph，不依赖真实 Weaviate / Neo4j。
"""
from unittest.mock import MagicMock

import pytest

from src.service.qa_engine.retriever import QARetriever, RetrievedContext


# ───────── fixtures ─────────

@pytest.fixture
def mock_interpretation_store():
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
async def test_retrieve_returns_context_object(mock_interpretation_store, mock_graph):
    r = QARetriever(interpretation_store=mock_interpretation_store, graph=mock_graph)
    ctx = await r.retrieve(question="存款开户的设计逻辑", project_id="deposit", top_k=5)
    assert isinstance(ctx, RetrievedContext)
    assert ctx.question == "存款开户的设计逻辑"
    assert ctx.project_id == "deposit"


@pytest.mark.asyncio
async def test_retrieve_passes_project_id_filter(mock_interpretation_store, mock_graph):
    """retriever 必须把 project_id 透传给 interpretation_store（多工程隔离的关键）。"""
    r = QARetriever(interpretation_store=mock_interpretation_store, graph=mock_graph)
    await r.retrieve(question="x", project_id="deposit", top_k=5)
    # interpretation_store 被调用时应该带 project_id
    call_kwargs = mock_interpretation_store.search_method_hits_by_text.call_args.kwargs
    assert call_kwargs.get("project_id") == "deposit"


@pytest.mark.asyncio
async def test_retrieve_top_k_passed(mock_interpretation_store, mock_graph):
    r = QARetriever(interpretation_store=mock_interpretation_store, graph=mock_graph)
    await r.retrieve(question="x", project_id="p", top_k=10)
    call_kwargs = mock_interpretation_store.search_method_hits_by_text.call_args.kwargs
    assert call_kwargs.get("limit") == 10


@pytest.mark.asyncio
async def test_retrieve_extracts_callees_for_top_candidates(mock_interpretation_store, mock_graph):
    r = QARetriever(interpretation_store=mock_interpretation_store, graph=mock_graph)
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
    r = QARetriever(interpretation_store=bs, graph=mock_graph)
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
    r = QARetriever(interpretation_store=bs, graph=g)
    ctx = await r.retrieve(question="不存在的功能", project_id="p", top_k=5)
    assert ctx.entry_candidates == []
    assert ctx.callees_by_entry == {}


# ───────── skills 分支（v1.1）─────────


@pytest.mark.asyncio
async def test_architecture_path_does_not_rerank_candidates() -> None:
    """architecture 默认路径不重排：entry_candidates 顺序与 store 返回一致。"""
    bs = MagicMock()
    bs.search_method_hits_by_text.return_value = [
        {"entity_id": "M1", "level": "api", "summary_text": "x", "score": 0.9},
        {"entity_id": "C1", "level": "class", "summary_text": "y", "score": 0.7},
    ]
    g = MagicMock()
    g.successors.return_value = []
    g.predecessors.return_value = []
    r = QARetriever(interpretation_store=bs, graph=g)
    ctx = await r.retrieve(question="x", project_id="p", top_k=5)  # 默认 architecture
    # 顺序应该跟 store 返回值一致
    assert [c["entity_id"] for c in ctx.entry_candidates] == ["M1", "C1"]


@pytest.mark.asyncio
async def test_architecture_path_does_not_extract_summary_tables() -> None:
    """architecture 默认路径不抽表：table_access_by_entry 来自图谱边，不解析 summary_text。"""
    bs = MagicMock()
    bs.search_method_hits_by_text.return_value = [
        {"entity_id": "M1", "level": "api", "summary_text": "查询 vets 表 写入 visits 表"}
    ]
    g = MagicMock()
    g.successors.return_value = []
    g.predecessors.return_value = []
    r = QARetriever(interpretation_store=bs, graph=g)
    ctx = await r.retrieve(question="x", project_id="p", top_k=5)  # default architecture
    # architecture 不抽 summary_text 里的表；图谱没边 → 空列表
    assert ctx.table_access_by_entry["M1"] == []


@pytest.mark.asyncio
async def test_retrieve_graph_failure_does_not_crash(mock_interpretation_store):
    """图查询出错（如节点不存在）时不能整个流程崩，应该静默跳过。"""
    g = MagicMock()
    g.successors.side_effect = Exception("node not found")
    g.predecessors.return_value = []
    r = QARetriever(interpretation_store=mock_interpretation_store, graph=g)
    ctx = await r.retrieve(question="x", project_id="p", top_k=5)
    # 候选还在
    assert len(ctx.entry_candidates) == 2
    # callees 取不到，对应条目可以缺省或为空列表，不能让整个流程挂
    # （实现里要 try/except）


# ───────── _bfs_edges 调用链降噪（调用图质量优化）─────────


def test_bfs_edges_skips_noise_children():
    """BFS 多跳保边时跳过 getter/setter / MyBatis 噪声子节点，
    把 max_edges 预算留给业务调用（[[召回链路缺陷诊断与修复方案]] 调用链图质量优化）。"""
    # 业务入口 → [业务Service, setter噪声]；业务Service → [setter噪声, Mapper.insert]
    succ = {
        "com.x.controller.MemberController::register#(p)": [
            "com.x.service.MemberService::register#(p)",
            "com.x.model.UmsMember::setStatus#(Integer)",  # accessor 噪声
        ],
        "com.x.service.MemberService::register#(p)": [
            "com.x.model.UmsMember::setPassword#(String)",  # accessor 噪声
            "com.x.mapper.MemberMapper::insert#(m)",        # 业务 DB 写
        ],
    }
    g = MagicMock()
    g.successors.side_effect = lambda n: succ.get(n, [])
    r = QARetriever(interpretation_store=MagicMock(), graph=g)
    edges = r._bfs_edges(
        "com.x.controller.MemberController::register#(p)", max_depth=2, max_edges=25
    )
    pairs = set(edges)
    # 业务边保留（含多跳）
    assert ("com.x.controller.MemberController::register#(p)",
            "com.x.service.MemberService::register#(p)") in pairs
    assert ("com.x.service.MemberService::register#(p)",
            "com.x.mapper.MemberMapper::insert#(m)") in pairs
    # setter 噪声子节点的边被跳过
    assert not any("setStatus" in to or "setPassword" in to for (_f, to) in edges)


