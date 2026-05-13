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


# ───────── skills 分支（v1.1）─────────


@pytest.mark.asyncio
async def test_dependency_skill_expands_call_chain_to_depth_2():
    """skill_id='dependency' 时，对每个 top 候选做 depth-2 BFS 拓展（覆盖 callers + callees）。

    用例图：
                Y → Z → A → B → C
              （←upstream）（→downstream）

    Architecture（默认）：只取 A 的 1 跳 → callees=[B], callers=[Z]
    Dependency：拓展到 2 跳 → callees 包含 B 和 C，callers 包含 Z 和 Y
    """
    # 构造一个简单的调用链 mock
    bs = MagicMock()
    # 唯一候选：A
    bs.search_method_hits_by_text.return_value = [
        {"entity_id": "A", "summary_text": "入口", "level": "api"}
    ]

    g = MagicMock()
    # successors：A→B→C；其它节点 → 空（不再拓展）
    # `side_effect` 当 mock 被调用时根据参数动态返回值（比 return_value 灵活）
    def succ(nid: str, rel_type=None) -> list[str]:
        return {"A": ["B"], "B": ["C"]}.get(nid, [])
    g.successors.side_effect = succ
    # predecessors：A←Z←Y
    def pred(nid: str, rel_type=None) -> list[str]:
        return {"A": ["Z"], "Z": ["Y"]}.get(nid, [])
    g.predecessors.side_effect = pred

    r = QARetriever(business_store=bs, graph=g)

    # 1) Architecture（默认 skill_id）只取 1 跳
    ctx_arch = await r.retrieve(question="A 怎么实现", project_id="p", top_k=5)
    assert ctx_arch.callees_by_entry["A"] == ["B"]
    assert ctx_arch.callers_by_entry["A"] == ["Z"]

    # 2) Dependency 拓展到 2 跳
    ctx_dep = await r.retrieve(
        question="A 调用了什么", project_id="p", top_k=5, skill_id="dependency"
    )
    # 顺序：先 1 跳，再 2 跳；C 必须在 B 之后；Y 必须在 Z 之后
    assert ctx_dep.callees_by_entry["A"] == ["B", "C"]
    assert ctx_dep.callers_by_entry["A"] == ["Z", "Y"]


@pytest.mark.asyncio
async def test_business_skill_promotes_class_and_module_level_candidates() -> None:
    """skill_id='business' 时，class/module 级候选要排在 method 级前面。

    原因：业务规则 / 约束类问题，class/module 的 summary_text 更聚焦"是什么、有什么规则"，
    method 级聚焦"怎么实现"，两者放反序会让 LLM 答案偏技术化。
    """
    # 模拟一个混合层级的检索结果（按 score 排序）
    bs = MagicMock()
    bs.search_method_hits_by_text.return_value = [
        # method 分高，但业务问题里不该优先
        {"entity_id": "M1", "level": "api", "summary_text": "技术细节", "score": 0.9},
        {"entity_id": "C1", "level": "class", "summary_text": "业务能力 X 的入口", "score": 0.7},
        {"entity_id": "MOD1", "level": "module", "summary_text": "模块综述", "score": 0.6},
        {"entity_id": "M2", "level": "method", "summary_text": "另一个 method 细节", "score": 0.5},
    ]
    g = MagicMock()
    g.successors.return_value = []
    g.predecessors.return_value = []

    r = QARetriever(business_store=bs, graph=g)
    ctx = await r.retrieve(
        question="有什么业务规则", project_id="p", top_k=5, skill_id="business"
    )

    # 重排后顺序：class+module 排前，method+api 排后；同组内保持原相对顺序
    entity_ids = [c["entity_id"] for c in ctx.entry_candidates]
    assert entity_ids == ["C1", "MOD1", "M1", "M2"], f"实际顺序：{entity_ids}"


@pytest.mark.asyncio
async def test_business_skill_does_not_affect_default_ordering() -> None:
    """非 business skill 时（即 default 或其它），不应触发重排。"""
    bs = MagicMock()
    bs.search_method_hits_by_text.return_value = [
        {"entity_id": "M1", "level": "api", "summary_text": "x", "score": 0.9},
        {"entity_id": "C1", "level": "class", "summary_text": "y", "score": 0.7},
    ]
    g = MagicMock()
    g.successors.return_value = []
    g.predecessors.return_value = []
    r = QARetriever(business_store=bs, graph=g)
    ctx = await r.retrieve(question="x", project_id="p", top_k=5)  # 默认 architecture
    # 顺序应该跟 store 返回值一致
    assert [c["entity_id"] for c in ctx.entry_candidates] == ["M1", "C1"]


@pytest.mark.asyncio
async def test_data_flow_skill_extracts_tables_from_summary_text() -> None:
    """skill_id='data-flow' 时，summary_text 里的"<word> 表"模式要被提取到 table_access。

    背景：PetClinic 等真实项目的图谱里没有 `accesses_table` 关系，
    但 LLM 写的 summary_text 经常提到"查询 vets 表 / 写入 visits 表"。
    data-flow 问题里把这些信息做出来比当成普通文本扔给 LLM 强。
    """
    bs = MagicMock()
    bs.search_method_hits_by_text.return_value = [
        {
            "entity_id": "M1",
            "level": "api",
            "summary_text": "该接口 查询 vets 表 并 写入 visits 表，最后更新 owners 表",
        }
    ]
    g = MagicMock()
    # 图谱里没有 accesses_table 边
    g.successors.return_value = []
    g.predecessors.return_value = []

    r = QARetriever(business_store=bs, graph=g)
    ctx = await r.retrieve(
        question="数据怎么流的", project_id="p", top_k=5, skill_id="data-flow"
    )

    # table_access 应该包含从 summary_text 抽出的 3 张表
    table_ids = {t["table_id"] for t in ctx.table_access_by_entry["M1"]}
    # 集合断言：包含 vets / visits / owners（顺序不重要）
    assert table_ids >= {"vets", "visits", "owners"}, f"实际：{table_ids}"


@pytest.mark.asyncio
async def test_data_flow_skill_does_not_extract_for_other_skills() -> None:
    """非 data-flow skill 时不做 summary_text 提取（避免给所有问题加噪音）。"""
    bs = MagicMock()
    bs.search_method_hits_by_text.return_value = [
        {"entity_id": "M1", "level": "api", "summary_text": "查询 vets 表 写入 visits 表"}
    ]
    g = MagicMock()
    g.successors.return_value = []
    g.predecessors.return_value = []
    r = QARetriever(business_store=bs, graph=g)
    ctx = await r.retrieve(question="x", project_id="p", top_k=5)  # default architecture
    # architecture 不抽 summary_text 里的表；图谱没边 → 空列表
    assert ctx.table_access_by_entry["M1"] == []


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
