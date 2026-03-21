"""词面重排：英文整词匹配，避免短词子串污染。"""
from src.knowledge.business_question_lexical_rerank import (
    ascii_lexical_hit,
    soft_token_flat_for_ascii_match,
)


def test_soft_token_splits_camel_case() -> None:
    flat = soft_token_flat_for_ascii_match("deleteOrder")
    assert "delete" in flat
    assert "order" in flat


def test_or_does_not_match_inside_order() -> None:
    flat = soft_token_flat_for_ascii_match("cancelTimeOutOrder")
    assert ascii_lexical_hit("order", flat)
    assert not ascii_lexical_hit("or", flat)


def test_standalone_or_matches() -> None:
    flat = soft_token_flat_for_ascii_match("or callback")
    assert ascii_lexical_hit("or", flat)
