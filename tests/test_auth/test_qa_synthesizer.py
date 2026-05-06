"""验证 QASynthesizer：调 LLM 合成 6 段式答案 + JSON 解析容错。

LLM 是 mock 的（测试不打真模型）；
重点验证 JSON 解析、降级、context 转换。
"""
import json
from unittest.mock import AsyncMock

import pytest

from src.service.qa_engine.retriever import RetrievedContext
from src.service.qa_engine.synthesizer import QASynthesizer, SynthesizedAnswer


def _ok_answer_json() -> str:
    """合法的 6 段式 JSON 输出（用 ```json fenced 包裹）。"""
    payload = {
        "sections": [
            {"type": "overview", "title": "业务概述",
             "content": "存款开户是核心流程", "references": []},
            {"type": "entry_point", "title": "入口方法",
             "content": "[method://m1|DepositController.openAccount()]",
             "references": [{"entity_id": "method://m1",
                            "display_text": "DepositController.openAccount()",
                            "kind": "method"}]},
        ]
    }
    return f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```"


def _make_ctx() -> RetrievedContext:
    return RetrievedContext(
        question="存款开户的设计逻辑",
        project_id="deposit",
        entry_candidates=[{"entity_id": "method://m1", "summary_text": "x", "level": "api"}],
    )


# ───────── happy path ─────────

@pytest.mark.asyncio
async def test_synthesize_returns_structured_answer():
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=_ok_answer_json())

    s = QASynthesizer(llm_provider=llm)
    result = await s.synthesize(_make_ctx())

    assert isinstance(result, SynthesizedAnswer)
    assert len(result.sections) == 2
    assert result.sections[0]["type"] == "overview"
    assert result.sections[1]["type"] == "entry_point"
    assert result.token_usage > 0


@pytest.mark.asyncio
async def test_synthesize_passes_system_and_user_to_llm():
    """LLM 调用必须分别传 system + user 两个参数。"""
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=_ok_answer_json())
    s = QASynthesizer(llm_provider=llm)
    await s.synthesize(_make_ctx())

    call = llm.complete.call_args
    assert "system" in call.kwargs
    assert "user" in call.kwargs
    # system prompt 包含核心约束
    assert "不允许编造" in call.kwargs["system"]
    # user prompt 包含用户问题
    assert "存款开户的设计逻辑" in call.kwargs["user"]


# ───────── JSON 解析容错 ─────────

@pytest.mark.asyncio
async def test_synthesize_handles_invalid_json_gracefully():
    """LLM 输出不是合法 JSON 时降级为单段 markdown。"""
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value="这是普通 markdown 不是 JSON")

    s = QASynthesizer(llm_provider=llm)
    result = await s.synthesize(_make_ctx())

    assert len(result.sections) == 1
    assert result.sections[0]["type"] == "overview"
    assert "markdown" in result.sections[0]["content"]


@pytest.mark.asyncio
async def test_synthesize_handles_json_without_fence():
    """LLM 输出没用 ```json fenced，直接是 JSON：也要能解析。"""
    payload = {"sections": [{"type": "overview", "title": "x", "content": "y", "references": []}]}
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=json.dumps(payload, ensure_ascii=False))

    s = QASynthesizer(llm_provider=llm)
    result = await s.synthesize(_make_ctx())
    assert len(result.sections) == 1
    assert result.sections[0]["content"] == "y"


@pytest.mark.asyncio
async def test_synthesize_filters_invalid_sections():
    """sections 里缺 type 或 content 字段的条目要过滤掉。"""
    bad = json.dumps({"sections": [
        {"type": "overview", "title": "ok", "content": "正常段", "references": []},
        {"title": "缺 type"},                   # invalid
        {"type": "rules"},                       # invalid (no content)
    ]}, ensure_ascii=False)
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=bad)

    s = QASynthesizer(llm_provider=llm)
    result = await s.synthesize(_make_ctx())
    # 只有 1 个合法 section
    assert len(result.sections) == 1
    assert result.sections[0]["content"] == "正常段"


# ───────── LLM 调用失败 ─────────

@pytest.mark.asyncio
async def test_synthesize_handles_llm_exception():
    """LLM 抛错时返回错误 section，不重新抛错。"""
    llm = AsyncMock()
    llm.complete = AsyncMock(side_effect=RuntimeError("LLM provider down"))

    s = QASynthesizer(llm_provider=llm)
    result = await s.synthesize(_make_ctx())
    assert len(result.sections) == 1
    assert result.sections[0]["type"] == "overview"
    assert "失败" in result.sections[0]["content"] or "LLM" in result.sections[0]["content"]


# ───────── 多轮对话 ─────────

@pytest.mark.asyncio
async def test_synthesize_with_history():
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=_ok_answer_json())
    s = QASynthesizer(llm_provider=llm)

    history = [
        {"role": "user", "content": "上轮问题"},
        {"role": "assistant", "content": "上轮答案"},
    ]
    await s.synthesize(_make_ctx(), history=history)
    user_prompt = llm.complete.call_args.kwargs["user"]
    assert "对话历史" in user_prompt
    assert "上轮答案" in user_prompt
