# tests/test_auth/test_retriever_module_enrich.py
"""retrieve 的 architecture 分支给候选注入 module（best-effort）；chit-chat 分支不调 module_of。"""
import pytest
from src.service.qa_engine.retriever import QARetriever


class _Store:
    def __init__(self, hits):
        self._hits = hits
    def search_method_hits_by_text(self, *, text, project_id, limit=5):
        # 防御性浅拷贝每个候选 dict：生产里 composite 每次返回新 dict，
        # 此处镜像该契约，避免 retrieve 就地写 c["module"] 跨调用泄漏，保证测试隔离
        return [dict(h) for h in self._hits]


class _Graph:
    def __init__(self):
        self.module_calls = []
    def successors(self, entity_id, rel_type=None):
        return []
    def predecessors(self, entity_id, rel_type=None):
        return []
    def module_of(self, entity_id):
        self.module_calls.append(entity_id)
        return "mall-portal" if "Portal" in entity_id else "mall-admin"


@pytest.mark.asyncio
async def test_architecture_enriches_module():
    hits = [{"entity_id": "OmsPortalOrderController::generateOrder#()", "summary_text": "", "level": "code_entity", "score": 0.7}]
    g = _Graph()
    r = QARetriever(interpretation_store=_Store(hits), graph=g, recall_threshold=0.45)
    ctx = await r.retrieve(question="下单", project_id="mall-swarm")
    assert ctx.skill_id == "architecture"
    assert ctx.entry_candidates[0]["module"] == "mall-portal"  # 注入了 module
    assert "OmsPortalOrderController::generateOrder#()" in g.module_calls


@pytest.mark.asyncio
async def test_chit_chat_does_not_call_module_of():
    hits = [{"entity_id": "X::y#()", "summary_text": "", "level": "code_entity", "score": 0.2}]  # 低召回→chit-chat
    g = _Graph()
    r = QARetriever(interpretation_store=_Store(hits), graph=g, recall_threshold=0.45)
    ctx = await r.retrieve(question="你好", project_id="mall-swarm")
    assert ctx.skill_id == "chit-chat"
    assert ctx.entry_candidates == []
    assert g.module_calls == []  # chit-chat 分支不 enrich


@pytest.mark.asyncio
async def test_module_of_failure_sets_none():
    class _BoomGraph(_Graph):
        def module_of(self, entity_id):
            raise RuntimeError("boom")
    hits = [{"entity_id": "X::y#()", "summary_text": "", "level": "code_entity", "score": 0.7}]
    r = QARetriever(interpretation_store=_Store(hits), graph=_BoomGraph(), recall_threshold=0.45)
    ctx = await r.retrieve(question="q", project_id="p")
    assert ctx.entry_candidates[0]["module"] is None  # 单个失败置 None，不崩
