"""验证 ReAct 用的数据类型：ToolCall + LLMToolResponse。

设计要点：
  - ToolCall：LLM 想调一个工具时的请求结构（OpenAI tool_calls 抽象出来的）
  - LLMToolResponse：LLM 一轮返回（要么 content 是文本，要么 tool_calls 非空）
  - 两者都用 frozen dataclass：不可变，方便 hash / 多线程安全
"""
import pytest

# 待实现的类型；现在 import 会失败 → RED
from src.service.qa_engine.llm_types import ToolCall, LLMToolResponse


def test_tool_call_has_id_name_arguments() -> None:
    """ToolCall(id=..., name=..., arguments=dict)。"""
    tc = ToolCall(id="call_abc", name="ke_callees", arguments={"entity_id": "M1"})
    assert tc.id == "call_abc"
    assert tc.name == "ke_callees"
    # arguments 是 dict（不是 JSON 字符串）—— 调用方负责把 OpenAI 原始字符串 json.loads 好
    assert tc.arguments == {"entity_id": "M1"}


def test_tool_call_is_frozen() -> None:
    """frozen=True：构造后不能改字段（防止流转过程被偷偷改）。"""
    tc = ToolCall(id="x", name="y", arguments={})
    # `pytest.raises` + `FrozenInstanceError` 验证 dataclass 不可变
    from dataclasses import FrozenInstanceError
    with pytest.raises(FrozenInstanceError):
        tc.id = "modified"


def test_llm_tool_response_with_final_content() -> None:
    """LLM 给出最终回答时：content 非空，tool_calls 空。"""
    resp = LLMToolResponse(content="最终答案 JSON", tool_calls=[])
    assert resp.content == "最终答案 JSON"
    assert resp.tool_calls == []
    # 便捷判断：has_tool_calls() 帮 ReAct 循环知道继不继续
    assert resp.has_tool_calls() is False


def test_llm_tool_response_with_tool_calls() -> None:
    """LLM 想调工具：content 可能为 None，tool_calls 非空。"""
    calls = [ToolCall(id="c1", name="ke_search", arguments={"query": "x", "project_id": "p"})]
    resp = LLMToolResponse(content=None, tool_calls=calls)
    assert resp.content is None
    assert len(resp.tool_calls) == 1
    assert resp.has_tool_calls() is True
