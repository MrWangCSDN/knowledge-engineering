# tests/test_auth/test_prompt_module_label.py
"""
build_user_prompt：候选带 module 渲染出 (模块: x)；None 省略；含模块判断指引。

测试范围：
  - test_renders_module_label  : module 字段有值 → 渲染 (模块: x) 及指引
  - test_omits_module_when_none: module=None    → 不渲染、不追加指引
  - test_no_module_key_safe    : 候选无 module 键 → 不报错、向后兼容
"""

# 从 prompts 模块导入被测函数
from src.service.qa_engine.prompts import build_user_prompt


def _ctx(cands):
    """
    构造最小 context 字典，只传必要键，减少测试噪声。

    Args:
        cands: entry_candidates 列表
    Returns:
        符合 build_user_prompt 期望结构的 dict
    """
    # callees_by_entry / callers_by_entry / table_access_by_entry 均为空 dict，
    # skill_id 固定用 architecture，让模板走常规分支
    return {
        "entry_candidates": cands,
        "callees_by_entry": {},
        "callers_by_entry": {},
        "table_access_by_entry": {},
        "skill_id": "architecture",
    }


def test_renders_module_label():
    """module 字段有值时，应在对应候选行追加 (模块: mall-portal) 并在尾部追加指引。"""
    # 构造一个带 module 的候选
    p = build_user_prompt(
        "下单流程",
        _ctx([
            {
                "entity_id": "OmsPortalOrderController::generateOrder#()",
                "level": "code_entity",
                "summary_text": "",
                "module": "mall-portal",  # 有 module，期望被渲染
            },
        ]),
    )
    # 候选行应含 (模块: mall-portal) 标注
    assert "(模块: mall-portal)" in p
    # 指引文本应同时含 mall-portal、mall-admin、臆断三个关键词
    assert "mall-portal" in p
    assert "mall-admin" in p
    assert "臆断" in p


def test_omits_module_when_none():
    """module=None 时，不应渲染 (模块: ...) 串，也不应追加模块判断指引。"""
    p = build_user_prompt(
        "q",
        _ctx([
            {
                "entity_id": "X::y#()",
                "level": "code_entity",
                "summary_text": "",
                "module": None,  # 显式 None → 不渲染
            },
        ]),
    )
    # 不应出现任何 (模块: 的片段
    assert "(模块:" not in p
    # 全 None 时不追加模块指引，避免无信息噪声
    assert "判断前台/后台" not in p


def test_no_module_key_safe():
    """候选 dict 根本没有 module 键，也不应报错（向后兼容旧数据）。"""
    # 故意不带 module 键
    p = build_user_prompt(
        "q",
        _ctx([
            {
                "entity_id": "X::y#()",
                "level": "code_entity",
                "summary_text": "",
                # 无 module 键
            },
        ]),
    )
    # 既不渲染 (模块: 串，也不加指引
    assert "(模块:" not in p


def test_partial_module_candidates():
    """5 个候选中只有部分带 module：只有对应行渲染标注，但指引仍追加（生产最常见的跨模块召回场景）。"""
    p = build_user_prompt("q", _ctx([
        {"entity_id": "A::a#()", "level": "method", "summary_text": "", "module": "mall-portal"},
        {"entity_id": "B::b#()", "level": "method", "summary_text": "", "module": None},
    ]))
    assert "(模块: mall-portal)" in p     # 带 module 的行渲染出标注
    assert "判断前台/后台" in p           # 有至少一个 module → 指引出现
    assert p.count("(模块:") == 1         # 只有 1 行渲染模块（另一行 module=None 省略）
