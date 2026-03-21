from src.knowledge.method_entity_id_normalize import (
    method_entity_id_variants,
    normalize_method_entity_id,
)


def test_normalize_method_double_slash() -> None:
    assert normalize_method_entity_id("method//abc12") == "method://abc12"


def test_normalize_already_colon() -> None:
    assert normalize_method_entity_id("method://abc12") == "method://abc12"


def test_variants_roundtrip() -> None:
    v = method_entity_id_variants("method://x")
    assert "method://x" in v
    assert "method//x" in v
