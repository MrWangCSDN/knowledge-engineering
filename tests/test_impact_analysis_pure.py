from src.app.views.scene_template_room.impact_analysis_pure import (
    ImpactNodeRow,
    build_impact_node_rows,
    compute_impact_closure_set,
    impact_type_histogram_top,
    sorted_impact_node_rows,
    take_top_n,
)


def test_build_sort_histogram_topn():
    def get_node(nid: str):
        return {"method://a": {"entity_type": "method", "name": "foo"}, "class://b": {"entity_type": "class"}}.get(
            nid
        )

    closure = {"method://a", "class://b"}
    rows = build_impact_node_rows(closure, get_node)
    assert len(rows) == 2
    hist = impact_type_histogram_top(rows, top_k=8)
    assert dict(hist).get("class") == 1
    assert dict(hist).get("method") == 1
    ordered = sorted_impact_node_rows(rows)
    assert ordered[0].entity_type <= ordered[1].entity_type
    assert len(take_top_n(ordered, 1)) == 1


def test_compute_impact_closure_set_with_callable():
    class B:
        def impact_closure(self, start, direction, max_depth):
            if direction == "down":
                return [start, "x"]
            return [start, "y"]

    s = compute_impact_closure_set(B(), "s", mode="down", max_depth=3)
    assert s == {"s", "x"}
    s2 = compute_impact_closure_set(B(), "s", mode="both", max_depth=3)
    assert s2 == {"s", "x", "y"}


def test_compute_impact_closure_set_fallback():
    s = compute_impact_closure_set(object(), "only", mode="down", max_depth=1)
    assert s == {"only"}
