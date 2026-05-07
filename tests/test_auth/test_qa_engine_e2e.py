"""端到端 QA Engine 测试 — 需要真实 LLM + Weaviate。

⚠️ 默认 CI 跳过（pyproject.toml 的 addopts = "-m 'not e2e'"）。
显式跑：
    cd /Users/java/knowledge-engineering-auth
    source venv/bin/activate
    KE_DB_URL=mysql+asyncmy://... \\
    LLM_PROVIDER=tongyi \\
    DASHSCOPE_API_KEY=... \\
    pytest tests/test_auth/test_qa_engine_e2e.py -v -m e2e -s

用途：
  - 集成检查：retriever + synthesizer + LLM + Weaviate 全链路通
  - W4 联调时跑一次确认
  - W6 调 prompt 时反复跑（看答案质量）

跨仓依赖：需要 knowledge-engineering 主仓的 BusinessInterpretationStore + KnowledgeGraph。
当前以 skip 形式占位；W4 Task 4.2 真正接入运行时实例后再 enable。
"""
import os

import pytest

from src.service.qa_engine import QARetriever, QASynthesizer


# 这些 fixtures 当前都 skip。等 W4 把主仓 store 接进来后实现。

@pytest.fixture
def real_business_store():
    pytest.skip("W4 接入主仓 BusinessInterpretationStore 后启用")


@pytest.fixture
def real_graph():
    pytest.skip("W4 接入主仓 KnowledgeGraph 后启用")


@pytest.fixture
def real_llm_provider():
    pytest.skip("W4 接入主仓 LLMProviderFactory 后启用")


# ───────── 端到端测试 ─────────

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_e2e_explain_question(real_llm_provider, real_business_store, real_graph):
    """端到端：问一个真实业务问题，验证答案质量基线。

    验收基线：
      - 至少 1 段（overview）
      - sections 内容非空
      - 不能出现"我不知道"之类的回避话术
      - LLM 调用 < 15s（不限制，但记录耗时）
    """
    retriever = QARetriever(business_store=real_business_store, graph=real_graph)
    synthesizer = QASynthesizer(llm_provider=real_llm_provider)

    ctx = await retriever.retrieve(
        question="存款开户的设计逻辑是怎样的？",
        project_id=os.getenv("KE_E2E_PROJECT_ID", "default"),
        top_k=5,
    )
    answer = await synthesizer.synthesize(ctx)

    # 至少有 overview
    assert any(s.get("type") == "overview" for s in answer.sections), \
        f"答案缺 overview 段：{answer.sections}"

    # 没有空 section
    for s in answer.sections:
        assert s.get("content", "").strip(), f"空 section：{s}"

    # 没有回避话术
    full_text = " ".join(s.get("content", "") for s in answer.sections)
    for bad in ["不知道", "无法回答", "抱歉，没法"]:
        assert bad not in full_text, f"出现回避话术 '{bad}'：{full_text[:200]}"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_e2e_no_match_question(real_llm_provider, real_business_store, real_graph):
    """问一个明显不存在的功能（如 '量子计算'），LLM 应该承认找不到。

    设计上 build_user_prompt 会告诉 LLM：
        "如果 context 不足以回答，只输出一个 overview 段说明未找到相关业务逻辑"
    """
    retriever = QARetriever(business_store=real_business_store, graph=real_graph)
    synthesizer = QASynthesizer(llm_provider=real_llm_provider)

    ctx = await retriever.retrieve(
        question="量子计算的原理是怎么实现的？",
        project_id=os.getenv("KE_E2E_PROJECT_ID", "default"),
        top_k=5,
    )
    answer = await synthesizer.synthesize(ctx)

    # 应该只输出一段（未找到）；不能编造一堆段落
    assert len(answer.sections) <= 2, \
        f"找不到时不应编造多段：{[s.get('type') for s in answer.sections]}"
