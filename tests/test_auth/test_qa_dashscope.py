"""验证 DashScopeProvider 的 tool calling 支持（响应解析层）。

只单测 OpenAI 兼容响应 → LLMToolResponse 的转换；
真的 HTTP 调用是 e2e 测试的事，不在这里跑。
"""
import json
import pytest

from src.service.qa_engine.llm_dashscope import DashScopeProvider
from src.service.qa_engine.llm_types import LLMToolResponse


# DashScope OpenAI-compatible 接口的真实响应样本（脱敏后）
_FINAL_ANSWER_RESPONSE = {
    "choices": [{
        "message": {
            "role": "assistant",
            "content": "这是最终答案",
        }
    }]
}

_TOOL_CALL_RESPONSE = {
    "choices": [{
        "message": {
            "role": "assistant",
            "content": None,  # LLM 调工具时 content 通常为 null
            "tool_calls": [
                {
                    "id": "call_abc123",
                    "type": "function",
                    "function": {
                        "name": "ke_callees",
                        # arguments 在 wire 上是 JSON 字符串（OpenAI 协议）
                        "arguments": '{"entity_id": "method//M1", "max_nodes": 5}',
                    },
                }
            ],
        }
    }]
}


def test_parse_final_answer_response() -> None:
    """LLM 给最终文本时：content 非空 + tool_calls 空。"""
    resp = DashScopeProvider._parse_tool_response(_FINAL_ANSWER_RESPONSE)
    assert isinstance(resp, LLMToolResponse)
    assert resp.content == "这是最终答案"
    assert resp.tool_calls == []
    assert resp.has_tool_calls() is False


def test_parse_tool_call_response() -> None:
    """LLM 想调工具时：tool_calls 非空 + arguments 已经 json.loads 成 dict。"""
    resp = DashScopeProvider._parse_tool_response(_TOOL_CALL_RESPONSE)
    assert resp.content is None
    assert len(resp.tool_calls) == 1

    tc = resp.tool_calls[0]
    assert tc.id == "call_abc123"
    assert tc.name == "ke_callees"
    # **关键**：arguments 是 dict 不是字符串（DashScopeProvider 帮我们 json.loads 好了）
    assert tc.arguments == {"entity_id": "method//M1", "max_nodes": 5}


def test_parse_response_missing_tool_calls_returns_empty_list() -> None:
    """`tool_calls` 字段缺失时（普通文本回答）→ 空 list，不抛 KeyError。"""
    resp = DashScopeProvider._parse_tool_response({
        "choices": [{"message": {"content": "x"}}]
    })
    assert resp.tool_calls == []


def test_parse_response_malformed_arguments_falls_back_to_empty_dict() -> None:
    """LLM 偶尔返回非合法 JSON 的 arguments（虽然 OpenAI 协议禁止但实际会发生）→ arguments 兜底为 {}。"""
    sample = {
        "choices": [{
            "message": {
                "content": None,
                "tool_calls": [{
                    "id": "call_x",
                    "type": "function",
                    "function": {
                        "name": "ke_search",
                        "arguments": "this is not json{{",  # 故意坏的
                    },
                }],
            }
        }]
    }
    resp = DashScopeProvider._parse_tool_response(sample)
    assert len(resp.tool_calls) == 1
    # arguments 解析失败 → 兜底空 dict，let LLM 在下一轮收到 missing-field 错误自我修复
    assert resp.tool_calls[0].arguments == {}


# ───────── v1.6 streaming: 增量行解析 ─────────


def test_parse_stream_chunk_extracts_content_delta() -> None:
    """OpenAI 流式响应单行格式：
        data: {"choices":[{"delta":{"content":"chunk1"},...}],...}

    `_parse_stream_chunk` 从一行 SSE 文本里抽 content delta；不含 content 时返回 None。
    """
    # 正常 content delta
    line = 'data: {"choices":[{"delta":{"content":"你好"},"finish_reason":null,"index":0}]}'
    assert DashScopeProvider._parse_stream_chunk(line) == "你好"

    # role 行（OpenAI 流首条通常是 {"delta":{"role":"assistant"}}，没 content）
    line2 = 'data: {"choices":[{"delta":{"role":"assistant"},"finish_reason":null,"index":0}]}'
    assert DashScopeProvider._parse_stream_chunk(line2) is None

    # 终止信号
    assert DashScopeProvider._parse_stream_chunk("data: [DONE]") is None

    # 空行 / 注释 / 不合法 JSON 都返回 None（不抛错）
    assert DashScopeProvider._parse_stream_chunk("") is None
    assert DashScopeProvider._parse_stream_chunk(":keep-alive") is None
    assert DashScopeProvider._parse_stream_chunk("data: {bad json") is None
