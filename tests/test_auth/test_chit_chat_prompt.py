"""_CHIT_CHAT_SYSTEM prompt 行为契约 characterization 测试。

设计：[[chit-chat-闲聊路径-设计]] §2.3, §5
v1.3：chit-chat 从"死板引导回 4 能力"扩为"通用编程助手直接答"。
prompt 内容无法做行为单测（要真调 LLM），用文本契约兜底防回归：
  - 旧死板规则文字不得残留
  - 新"直接回答通用技术问题"指令须存在
  - 产品问询仍保留 4 个 KG 能力介绍
"""
from src.service.qa_engine.prompts import _CHIT_CHAT_SYSTEM


def test_old_rigid_deflection_rule_removed():
    # v1.2.1 的死板规则原文，v1.3 必须删除
    assert "不要回答与代码工程无关的问题" not in _CHIT_CHAT_SYSTEM
    # 旧的"1-3 句"硬长度限制也删除（通用问题答案需要长度+代码块）
    assert "1-3 句" not in _CHIT_CHAT_SYSTEM


def test_general_tech_questions_answered_directly():
    # 新行为：必须明确指示"直接/完整回答通用编程/技术问题"
    assert "通用编程" in _CHIT_CHAT_SYSTEM
    # 必须允许代码块/不限长度（任一关键词出现即可）
    assert ("代码块" in _CHIT_CHAT_SYSTEM) or ("Markdown" in _CHIT_CHAT_SYSTEM)


def test_product_query_still_introduces_4_abilities():
    # 产品问询行为保留：4 个 KG 能力仍在 prompt 里
    for kw in ["业务规则", "调用链路", "数据流", "架构"]:
        assert kw in _CHIT_CHAT_SYSTEM


def test_greeting_behavior_kept():
    # 问候仍要求简短友好（关键词存在即可）
    assert "问候" in _CHIT_CHAT_SYSTEM
