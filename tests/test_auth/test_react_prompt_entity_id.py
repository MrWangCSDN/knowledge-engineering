# tests/test_auth/test_react_prompt_entity_id.py
"""验证 ReAct system prompt 的 entity_id 格式指引已切到 qualified_name，不再误导 LLM 用 canonical_v1。"""
import inspect
import src.service.qa_engine.react_synthesizer as rs


def test_prompt_uses_qualified_name_not_canonical_v1():
    # 取整个模块源码（prompt 是模块内的 f-string 模板）
    src = inspect.getsource(rs)
    assert "method//abc123" not in src, "prompt 还在用 canonical_v1 示例 method//abc123"
    assert "class//def456" not in src, "prompt 还在用 canonical_v1 示例 class//def456"
    # 应给出 qualified_name 形态示例（含 '::' 和 '#(' ）
    assert "::" in src and "#(" in src, "prompt 应给出 qualified_name 形态示例(Class::method#(params))"
