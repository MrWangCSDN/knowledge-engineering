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


# ───────── 嵌套 fence 解析（v1.1 修复） ─────────

@pytest.mark.asyncio
async def test_synthesize_includes_skill_hint_in_user_prompt():
    """RetrievedContext.skill_id 设了之后，user prompt 末尾要有"本题分类为 xxx"的提示词。

    LLM 看到这个后，在 Step 1 选视角时会更明确（dependency → dependency-map, etc）。
    """
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=_ok_answer_json())

    s = QASynthesizer(llm_provider=llm)
    ctx = RetrievedContext(
        question="OwnerController 调用了什么",
        project_id="p",
        entry_candidates=[{"entity_id": "M1", "summary_text": "x", "level": "api"}],
        skill_id="dependency",  # 新字段
    )
    await s.synthesize(ctx)

    user_prompt = llm.complete.call_args.kwargs["user"]
    # 关键断言：prompt 里出现 skill_id（不强求确切措辞，只要 dependency 字眼即可）
    assert "dependency" in user_prompt


# ───────── v1.6 streaming ─────────


@pytest.mark.asyncio
async def test_synthesize_stream_calls_on_token_for_each_chunk():
    """synthesize_stream(ctx, on_token) 应该：
      - 调 llm.complete_stream（不是 complete）
      - 每个 chunk 都调用 on_token(chunk)
      - 最终返回完整 SynthesizedAnswer（解析后的 sections）
    """
    # mock 一个 async generator yield 3 个 chunks
    async def fake_stream(*, system: str, user: str, **kwargs):
        # 把 _ok_answer_json 拆成 3 段模拟流
        full = _ok_answer_json()
        # 简单 3 段：前半 ```json + body 前半 + body 后半 + ``` 结束
        mid = len(full) // 2
        yield full[:mid]
        yield full[mid:]

    llm = AsyncMock()
    llm.complete_stream = fake_stream  # 直接换成 async generator function

    s = QASynthesizer(llm_provider=llm)

    # 收集 token 回调的实参
    tokens: list[str] = []

    async def on_token(t: str):
        tokens.append(t)

    result = await s.synthesize_stream(_make_ctx(), on_token=on_token)

    # 应该至少收到 2 个 chunk
    assert len(tokens) == 2
    # 拼起来等于完整原文
    assert "".join(tokens) == _ok_answer_json()
    # 最终解析成结构化答案
    assert isinstance(result, SynthesizedAnswer)
    assert len(result.sections) == 2  # _ok_answer_json 有 2 段


@pytest.mark.asyncio
async def test_synthesize_stream_handles_llm_error():
    """流式调用中途抛错 → 返回单段 error 答案，不传播异常给上层。"""
    async def fake_stream(*, system: str, user: str, **kwargs):
        # 先 yield 一点东西，然后抛
        yield "前半..."
        raise RuntimeError("LLM disconnected")

    llm = AsyncMock()
    llm.complete_stream = fake_stream

    s = QASynthesizer(llm_provider=llm)
    tokens: list[str] = []

    async def on_token(t: str):
        tokens.append(t)

    result = await s.synthesize_stream(_make_ctx(), on_token=on_token)

    # 第一段 chunk 收到了
    assert tokens == ["前半..."]
    # 但最终返回的是错误兜底答案，不抛
    assert isinstance(result, SynthesizedAnswer)
    assert len(result.sections) >= 1
    assert "失败" in result.sections[0]["content"] or "error" in result.sections[0]["content"].lower()


@pytest.mark.asyncio
async def test_synthesize_handles_nested_mermaid_fence():
    """回归：sections.content 嵌入 ```mermaid 子块时，外层 ```json fence 不能被首个内部 ``` 截断。

    历史 bug：parser 用 `split("```", 1)[0]` 从前向后找 fence 结束，
    遇到 ```mermaid 就会提前截断 → JSON 不完整 → 解析失败 → fallback 单段。
    修复后用 rsplit 找最后一个 fence。
    """
    # 模拟一个含 mermaid 子块的 LLM 输出
    raw_llm_output = """```json
{
  "sections": [
    {
      "type": "overview",
      "title": "业务概述",
      "content": "测试嵌套 fence 的稳健性",
      "references": []
    },
    {
      "type": "call_chain",
      "title": "调用链",
      "content": "```mermaid\\ngraph LR\\n  A --> B\\n```",
      "references": []
    }
  ]
}
```"""
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=raw_llm_output)
    s = QASynthesizer(llm_provider=llm)
    result = await s.synthesize(_make_ctx())

    # 修复后：应该解析出 2 段（overview + call_chain）
    assert len(result.sections) == 2, f"期望 2 段，实际 {len(result.sections)} 段：{result.sections}"
    assert result.sections[0]["type"] == "overview"
    assert result.sections[1]["type"] == "call_chain"
    assert "mermaid" in result.sections[1]["content"]
