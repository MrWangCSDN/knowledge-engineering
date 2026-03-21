from src.knowledge.method_table_access_service import (
    GraphWalkPredecessorConfig,
    GraphWalkSuccessorConfig,
    _filter_ids_excluding_prefixes,
)


def test_successor_presets():
    d = GraphWalkSuccessorConfig.method_to_table_default()
    assert d.calls_only is False
    assert "implements" in d.excluded_edge_rel_types
    assert "term://" in d.excluded_target_id_prefixes
    c = GraphWalkSuccessorConfig.calls_only_default()
    assert c.calls_only is True
    assert c.excluded_target_id_prefixes == ()


def test_predecessor_preset():
    p = GraphWalkPredecessorConfig.table_to_method_default()
    assert "implements" in p.excluded_edge_rel_types
    assert "domain://" in p.excluded_target_id_prefixes


def test_filter_ids_excluding_prefixes():
    ids = ["method://a", "term://x", "class://c"]
    out = _filter_ids_excluding_prefixes(ids, ("term://",))
    assert out == ["method://a", "class://c"]
