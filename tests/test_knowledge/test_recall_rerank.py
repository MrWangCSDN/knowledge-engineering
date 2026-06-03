"""召回降噪+加权纯函数单测。设计 [[召回降噪加权-设计]]。"""
from src.knowledge.recall_rerank import classify_entity, rerank_and_filter, is_callchain_noise


def test_classify_drop_boilerplate():
    assert classify_entity("com.macro.mall.mapper.OmsOrderMapper::Base_Column_List#()") == "drop"
    assert classify_entity("com.macro.mall.model.OmsOrderExample::andIdEqualTo#(Longvalue)") == "drop"


def test_classify_demote_lowvalue():
    assert classify_entity("OmsOrder::getStatus#()") == "demote"
    assert classify_entity("OmsOrder::setStatus#(Integerstatus)") == "demote"
    assert classify_entity("OmsOrder::isDeleted#()") == "demote"
    assert classify_entity("com.macro.mall.mapper.OmsOrderMapper::selectByExample#(OmsOrderExampleex)") == "demote"


def test_classify_boost_business():
    assert classify_entity("OmsPortalOrderController::generateOrder#(OrderParamp)") == "boost"
    assert classify_entity("OmsPortalOrderServiceImpl::cancelOrder#(Longid)") == "boost"
    assert classify_entity("OmsPortalOrderService::generateOrder#(OrderParamp)") == "boost"


def test_classify_neutral_keeps_real_db_writes():
    assert classify_entity("com.macro.mall.mapper.OmsOrderMapper::insert#(OmsOrderrow)") == "neutral"
    assert classify_entity("OmsOrderMapper::updateByPrimaryKeySelective#(OmsOrderrow)") == "neutral"
    assert classify_entity("OmsOrderMapper::selectByPrimaryKey#(Longid)") == "neutral"


def test_classify_malformed_is_neutral():
    assert classify_entity("weirdid") == "neutral"
    assert classify_entity("") == "neutral"


def test_classify_issue_prefix_not_demoted():
    # "issueRefund" 不是 is-访问器（is 后非大写）→ 不应误降；类名 Service → boost
    assert classify_entity("RefundService::issueRefund#()") == "boost"


def test_rerank_drops_boilerplate():
    hits = [
        ("com.macro.mall.mapper.OmsOrderMapper::Base_Column_List#()", 0.70),
        ("OmsOrderExample::andIdEqualTo#(Longv)", 0.69),
        ("OmsPortalOrderServiceImpl::generateOrder#(OrderParamp)", 0.60),
    ]
    out = rerank_and_filter(hits, limit=5)
    ids = [e for e, _ in out]
    assert "OmsPortalOrderServiceImpl::generateOrder#(OrderParamp)" in ids
    assert all("Base_Column_List" not in e and "Example::" not in e for e in ids)


def test_rerank_boost_outranks_demote_on_close_scores():
    hits = [
        ("OmsOrder::getStatus#()", 0.60),                                  # demote → 0.55
        ("OmsPortalOrderController::generateOrder#(OrderParamp)", 0.58),    # boost → 0.63
    ]
    out = rerank_and_filter(hits, limit=5)
    assert out[0][0] == "OmsPortalOrderController::generateOrder#(OrderParamp)"


def test_rerank_preserves_original_score():
    hits = [("OmsPortalOrderController::generateOrder#(OrderParamp)", 0.58)]
    out = rerank_and_filter(hits, limit=5)
    assert out[0] == ("OmsPortalOrderController::generateOrder#(OrderParamp)", 0.58)


def test_rerank_truncates_to_limit():
    hits = [(f"X::m{i}#()", 0.5 - i * 0.01) for i in range(10)]
    out = rerank_and_filter(hits, limit=3)
    assert len(out) == 3


def test_rerank_empty_after_filter_falls_back_to_original():
    hits = [
        ("com.macro.mall.mapper.AMapper::Base_Column_List#()", 0.40),
        ("BExample::andXEqualTo#(Xv)", 0.39),
    ]
    out = rerank_and_filter(hits, limit=5)
    assert out == hits[:5]


# ───────── is_callchain_noise（调用链图降噪，复用 classify_entity）─────────


def test_callchain_noise_filters_accessors_mybatis_and_wrappers():
    """getter/setter + MyBatis Example/CRUD + 结果包装类 → 调用图噪声。"""
    # accessor（drop/demote 里的 demote）
    assert is_callchain_noise("com.macro.mall.model.UmsMember::setStatus#(Integer)")
    assert is_callchain_noise("com.macro.mall.model.OmsOrder::getCreateTime#()")
    # MyBatis Example 条件类（drop：类名 endswith Example）
    assert is_callchain_noise("com.macro.mall.model.UmsMemberExample::createCriteria#()")
    assert is_callchain_noise("com.macro.mall.model.UmsMemberExample::or#()")
    # *ByExample 生成 CRUD（demote）
    assert is_callchain_noise("com.macro.mall.mapper.UmsMemberMapper::selectByExample#(E)")
    # 结果包装类（classify 归 neutral，但调用图里是噪声）
    assert is_callchain_noise("com.macro.mall.common.api.CommonResult::failed#(String)")


def test_callchain_noise_keeps_business_methods():
    """Controller/Service/Impl 业务方法 + Mapper 真实写操作 → 不是噪声，保留。"""
    assert not is_callchain_noise("com.macro.mall.controller.UmsMemberController::register#(p)")
    assert not is_callchain_noise("com.macro.mall.service.impl.UmsMemberServiceImpl::register#(p)")
    assert not is_callchain_noise("com.macro.mall.service.UmsMemberService::register#(p)")
    # Mapper 的 insert 是 neutral（真实 DB 写），不该被当噪声删
    assert not is_callchain_noise("com.macro.mall.mapper.UmsMemberMapper::insert#(m)")


def test_callchain_noise_malformed_is_not_noise():
    """空串 / 无 '::' 等异常格式 → 保守判定为非噪声（不误删）。"""
    assert not is_callchain_noise("")
    assert not is_callchain_noise("noColonsHere")
