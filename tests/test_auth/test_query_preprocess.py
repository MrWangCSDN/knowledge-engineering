"""clean_recall_query 单测：渲染指令 + 口水词剥离，纯代码语义保留。

设计：[[召回链路缺陷诊断与修复方案]] 快赢 A。
"""
# pytest 不需显式 import；直接写 test_ 函数即可被收集
from src.service.qa_engine.query_preprocess import clean_recall_query


def test_strips_render_directive_keeps_core():
    """"下单流程是怎么实现的，用流程图展示" → 提纯到核心 "下单流程"。"""
    out = clean_recall_query("下单流程是怎么实现的，用流程图展示")
    # 核心语义保留
    assert "下单流程" in out
    # 渲染指令 / 口水被剥离
    assert "流程图" not in out
    assert "展示" not in out
    assert "怎么实现" not in out


def test_strips_render_from_return_flow():
    """"用流程图展示退货流程" → "退货流程"（指令在前也能剥）。"""
    out = clean_recall_query("用流程图展示退货流程")
    assert "退货流程" in out
    assert "流程图" not in out and "展示" not in out


def test_clean_query_mostly_unchanged():
    """已经"干净"（无渲染/口水）的实体化问法基本保留，不破坏代码语义。"""
    out = clean_recall_query("generateOrder 的调用链路")
    assert "generateOrder" in out
    assert "调用链路" in out


def test_does_not_strip_liucheng_without_tu():
    """"下单流程" 中的"流程"不应被误当成"流程图"剥掉（必须带"图"才算渲染指令）。"""
    out = clean_recall_query("下单流程")
    assert out == "下单流程"


def test_all_directive_query_falls_back_to_original():
    """整句几乎全是指令/口水 → 清理后过短 → 回退原文，避免零信号召回。"""
    original = "帮我画个流程图展示一下"
    out = clean_recall_query(original)
    # 清理后核心几乎为空 → 回退原始问题（去首尾空白）
    assert out == original.strip()


def test_empty_and_whitespace_input():
    """空串 / 纯空白 / None 安全处理，永不抛。"""
    assert clean_recall_query("") == ""
    assert clean_recall_query("   ") == "   "  # 纯空白原样返回（strip 后为空走首个分支）
    assert clean_recall_query(None) == ""       # type: ignore[arg-type]  防御 None
