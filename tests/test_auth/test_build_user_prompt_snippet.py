"""build_user_prompt：有 candidate_code_snippets 时渲染源码 + 框架句；无则不渲染（不回归）。

source-first grounding P1 设计见 Obsidian [[业务问答-源码优先接地-P1设计]] §四.3。
"""
# 被测：把 RetrievedContext dict 渲染成 LLM user prompt 的函数
from src.service.qa_engine.prompts import build_user_prompt

# 最小候选 context：一个 method 候选 + skill_id（build_user_prompt 必读字段）
_BASE = {
    "entry_candidates": [{"entity_id": "A::m#()", "level": "method", "summary_text": "业务X"}],
    "skill_id": "architecture",
}


def test_renders_snippet_and_framing_when_present():
    """context 含某候选的 candidate_code_snippets → prompt 出现源码块 + 框架句。"""
    # 在 _BASE 基础上补一个候选源码（dict 解包合并语法 {**a, "k": v}）
    ctx = {**_BASE, "candidate_code_snippets": {"A::m#()": "public void m(){ dao.insert(); }"}}
    p = build_user_prompt("q", ctx)
    assert "真实源码片段" in p            # 源码块标题出现
    assert "dao.insert()" in p           # 源码内容逐行渲染进 prompt
    assert "以源码为准" in p              # 框架句出现（代码细节以源码为准）


def test_no_snippet_block_when_absent():
    """context 无 candidate_code_snippets → 不渲染源码块、不加框架句（不回归既有 prompt）。"""
    p = build_user_prompt("q", _BASE)
    assert "真实源码片段" not in p
    assert "以源码为准" not in p
