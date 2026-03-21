"""词面重排纯函数单测（不依赖 Streamlit）。"""
from src.knowledge.business_question_lexical_rerank import (
    merge_method_hits_max_score,
    query_terms_for_rerank,
    rerank_hits_by_lexical_overlap,
    soft_token_flat_for_ascii_match,
)


def test_merge_method_hits_max_score_dedup_and_max():
    tech = [("method://a", 0.5), ("method://b", 0.9)]
    biz = [("method://a", 0.8), ("method://c", 0.3)]
    out = merge_method_hits_max_score(tech, biz, pool=10)
    by_id = {m: s for m, s in out}
    assert by_id["method://a"] == 0.8
    assert "method://b" in by_id and "method://c" in by_id


def test_soft_token_flat_splits_camel():
    assert "order" in soft_token_flat_for_ascii_match("deleteOrder")
    assert "order" in soft_token_flat_for_ascii_match("order_global")


def test_query_terms_extracts_cjk_and_ascii():
    q = "用户注册后如何完成鉴权？order 流程"
    terms = query_terms_for_rerank(q)
    assert any("用户" in t or "注册" in t for t in terms) or len(terms) >= 1
    assert "order" in terms


def test_rerank_with_fake_get_node():
    """无解读库时仅靠节点 name/signature 也能跑通重排。"""
    nodes = {
        "method://x": {"name": "deleteOrder", "signature": "void deleteOrder()"},
        "method://y": {"name": "foo", "signature": "void bar()"},
    }

    def get_node(mid: str):
        return nodes.get(mid)

    hits = [("method://x", 0.5), ("method://y", 0.9)]
    out, avg_lex, terms, base_n, vocab_n = rerank_hits_by_lexical_overlap(
        "删除订单",
        None,
        hits,
        final_top_k=2,
        get_node=get_node,
        method_interpret_store=None,
        business_interpret_store=None,
    )
    assert len(out) <= 2
    assert base_n >= 0
    assert vocab_n == 0
    # 「订单」相关词应使 method://x 有机会被抬高（相对仅向量分）
    assert len(terms) >= 1
