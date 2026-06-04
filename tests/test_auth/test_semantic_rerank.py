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
