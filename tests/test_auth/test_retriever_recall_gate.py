"""召回门控：top1≥τ 走 architecture(查图)，top1<τ 走 chit-chat(空 ctx、不查图)。设计 [[召回门控路由-设计]]。"""
import pytest
from src.service.qa_engine.retriever import QARetriever


class _Store:
    """假 interpretation_store：按构造时给的 hits 返回（带 score）。"""
    def __init__(self, hits):
        self._hits = hits
    def search_method_hits_by_text(self, *, text, project_id, limit=5):
        return self._hits


class _Graph:
    """假 graph：记录是否被调用，用来断言低召回时不查图。"""
    def __init__(self):
        self.called = False
    def successors(self, entity_id, rel_type=None):
        self.called = True
        return ["A::b#()"]
    def predecessors(self, entity_id, rel_type=None):
        self.called = True
        return []


@pytest.mark.asyncio
async def test_high_recall_goes_architecture_and_walks_graph():
    """top1=0.7 ≥ τ(0.45) → architecture，candidates 非空，图导航被调用。"""
    hits = [{"entity_id": "Oms::gen#()", "summary_text": "", "level": "code_entity", "score": 0.7}]
    g = _Graph()
    r = QARetriever(interpretation_store=_Store(hits), graph=g, recall_threshold=0.45)
    ctx = await r.retrieve(question="下单流程", project_id="mall-swarm")
    assert ctx.skill_id == "architecture"
    assert ctx.entry_candidates and ctx.entry_candidates[0]["entity_id"] == "Oms::gen#()"
    assert ctx.recall_score == 0.7
    assert g.called is True


@pytest.mark.asyncio
async def test_low_recall_goes_chit_chat_and_skips_graph():
    """top1=0.3 < τ → chit-chat，candidates 空，图导航不被调用。"""
    hits = [{"entity_id": "Oms::gen#()", "summary_text": "", "level": "code_entity", "score": 0.3}]
    g = _Graph()
    r = QARetriever(interpretation_store=_Store(hits), graph=g, recall_threshold=0.45)
    ctx = await r.retrieve(question="你好", project_id="mall-swarm")
    assert ctx.skill_id == "chit-chat"
    assert ctx.entry_candidates == []
    assert ctx.recall_score == 0.3
    assert g.called is False


@pytest.mark.asyncio
async def test_threshold_boundary_inclusive():
    """top1 == τ 算过线（≥）。"""
    hits = [{"entity_id": "X::y#()", "summary_text": "", "level": "code_entity", "score": 0.5}]
    r = QARetriever(interpretation_store=_Store(hits), graph=_Graph(), recall_threshold=0.5)
    ctx = await r.retrieve(question="q", project_id="p")
    assert ctx.skill_id == "architecture"


@pytest.mark.asyncio
async def test_empty_hits_goes_chit_chat():
    """召回空 → chit-chat（top1 缺省 0.0 < τ）。"""
    r = QARetriever(interpretation_store=_Store([]), graph=_Graph(), recall_threshold=0.45)
    ctx = await r.retrieve(question="q", project_id="p")
    assert ctx.skill_id == "chit-chat"
    assert ctx.entry_candidates == []


@pytest.mark.asyncio
async def test_interp_hit_without_score_treated_as_pass():
    """解读库命中但无 score → 视为 1.0（强信号→过线 architecture）。设计 §7。"""
    hits = [{"entity_id": "I::j#()", "summary_text": "业务解读", "level": "method"}]  # 无 score
    r = QARetriever(interpretation_store=_Store(hits), graph=_Graph(), recall_threshold=0.45)
    ctx = await r.retrieve(question="q", project_id="p")
    assert ctx.skill_id == "architecture"
    assert ctx.recall_score == 1.0
