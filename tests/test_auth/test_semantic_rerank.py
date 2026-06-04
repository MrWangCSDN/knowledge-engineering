"""语义二次重排单测（[[召回二次重排-设计]]）。"""
from src.service.qa_engine.semantic_rerank import should_rerank


def test_should_rerank_confident_top1_skips():
    """top1 高(0.72)且与 top2 拉开(gap 0.12) → cosine 自信 → 不 rerank。"""
    assert should_rerank([{"score": 0.72}, {"score": 0.60}]) is False


def test_should_rerank_low_top1():
    """top1 低(0.5 < 0.6) → 不确定 → rerank。"""
    assert should_rerank([{"score": 0.5}, {"score": 0.3}]) is True


def test_should_rerank_small_margin():
    """top1 高(0.70)但与 top2 太近(gap 0.02 < 0.05) → 谁第一不明确 → rerank。"""
    assert should_rerank([{"score": 0.70}, {"score": 0.68}]) is True


def test_should_rerank_too_few_candidates():
    """候选 < 2 → 无可重排 → False。"""
    assert should_rerank([{"score": 0.5}]) is False
    assert should_rerank([]) is False


# ───────── rerank_candidates（调用 + 降级）─────────

import src.service.qa_engine.semantic_rerank as sr


def test_rerank_candidates_reorders_by_gte(monkeypatch):
    """env 开 + mock gte-rerank 返回新顺序 → 候选按之重排；score 字段不变。"""
    monkeypatch.setenv("KE_RECALL_RERANK", "1")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    monkeypatch.setattr(sr, "_gte_rerank", lambda q, d, k: [2, 0, 1])
    cands = [
        {"entity_id": "A", "summary_text": "a", "score": 0.6},
        {"entity_id": "B", "summary_text": "b", "score": 0.55},
        {"entity_id": "C", "summary_text": "c", "score": 0.5},
    ]
    out = sr.rerank_candidates("问题", cands)
    assert [c["entity_id"] for c in out] == ["C", "A", "B"]
    # score 字段不被改写（门控/前端显示用原始 cosine）
    by_id = {c["entity_id"]: c["score"] for c in out}
    assert by_id == {"A": 0.6, "B": 0.55, "C": 0.5}


def test_rerank_candidates_fallback_on_error(monkeypatch):
    """gte-rerank 抛异常 → 回退原 cosine 序，不抛。"""
    monkeypatch.setenv("KE_RECALL_RERANK", "1")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")

    def boom(*a, **k):
        raise RuntimeError("http down")

    monkeypatch.setattr(sr, "_gte_rerank", boom)
    cands = [{"entity_id": "A", "score": 0.6}, {"entity_id": "B", "score": 0.5}]
    assert sr.rerank_candidates("q", cands) == cands


def test_rerank_candidates_env_off(monkeypatch):
    """KE_RECALL_RERANK=0 → 直接原序。"""
    monkeypatch.setenv("KE_RECALL_RERANK", "0")
    cands = [{"entity_id": "A", "score": 0.6}, {"entity_id": "B", "score": 0.5}]
    assert sr.rerank_candidates("q", cands) == cands


def test_rerank_candidates_no_api_key(monkeypatch):
    """无 DASHSCOPE_API_KEY → 原序（不调用）。"""
    monkeypatch.setenv("KE_RECALL_RERANK", "1")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    cands = [{"entity_id": "A", "score": 0.6}, {"entity_id": "B", "score": 0.5}]
    assert sr.rerank_candidates("q", cands) == cands


def test_rerank_candidates_too_few(monkeypatch):
    """候选 < 2 → 原序。"""
    monkeypatch.setenv("KE_RECALL_RERANK", "1")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    one = [{"entity_id": "A", "score": 0.6}]
    assert sr.rerank_candidates("q", one) == one
