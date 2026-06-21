from src.service.indexing.states import (
    QUEUED, CLONING, BUILDING_GRAPH, CROSS_SERVICE, EMBEDDING, INTERPRETING, DONE, FAILED,
    PHASE_ORDER, is_terminal,
)


def test_phase_order():
    assert PHASE_ORDER == [CLONING, BUILDING_GRAPH, CROSS_SERVICE, EMBEDDING, INTERPRETING]


def test_terminal():
    assert is_terminal(DONE) and is_terminal(FAILED)
    assert not is_terminal(QUEUED) and not is_terminal(CLONING)
