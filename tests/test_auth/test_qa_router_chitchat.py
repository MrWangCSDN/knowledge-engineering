"""验证 SkillRouter v1.2 chit-chat 第 5 类路由。"""
import pytest
from unittest.mock import AsyncMock

from src.service.qa_engine.router import SkillRouter, _VALID_SKILL_IDS


def test_chit_chat_in_valid_skill_ids():
    """chit-chat 必须在合法 skill 集合里。"""
    assert "chit-chat" in _VALID_SKILL_IDS


def test_route_chit_chat_greeting():
    """「你好」命中 chit-chat 关键词。"""
    r = SkillRouter()
    d = r.route("你好")
    assert d.skill_id == "chit-chat"
    assert "你好" in d.matched_keywords
    assert d.source == "keyword"


def test_route_chit_chat_self_intro_query():
    """「你是谁」类产品问询命中 chit-chat。"""
    r = SkillRouter()
    assert r.route("你是谁").skill_id == "chit-chat"
    assert r.route("你叫什么").skill_id == "chit-chat"
    assert r.route("你能做什么").skill_id == "chit-chat"


def test_route_chit_chat_thanks_bye():
    """道谢/告别也命中 chit-chat。"""
    r = SkillRouter()
    assert r.route("谢谢").skill_id == "chit-chat"
    assert r.route("再见").skill_id == "chit-chat"
    assert r.route("拜拜").skill_id == "chit-chat"


def test_business_keyword_overrides_chit_chat():
    """关键词优先级：业务词 > chit-chat。

    「你好 OrderService 的调用」应该走 dependency（命中"调用"），不被「你好」吃掉。
    """
    r = SkillRouter()
    d = r.route("你好 OrderService 的调用")
    assert d.skill_id == "dependency", f"业务词应优先，实际 skill_id = {d.skill_id}"


def test_route_unknown_question_falls_back_to_chit_chat():
    """v1.2.1：未命中任何关键词 → 兜底 chit-chat（之前是 architecture）。

    "都走 chit-chat" 设计转变：业务问题应含明显关键词；不命中说明可能是
    社交语 / 模糊问询，走 chit-chat 让 LLM 引导回业务能力。
    """
    r = SkillRouter()
    d = r.route("abcdefgh")  # 不像问候也不像业务
    assert d.skill_id == "chit-chat"


@pytest.mark.asyncio
async def test_llm_fallback_can_choose_chit_chat():
    """LLM fallback 路径里 chit-chat 是合法选项之一。"""
    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value="chit-chat")  # 模拟 LLM 选了 chit-chat

    r = SkillRouter(llm_provider=mock_llm)
    d = await r.route_async("今天天气怎么样")  # 不命中任何关键词的奇怪问题
    assert d.skill_id == "chit-chat"
    assert d.source == "llm"
