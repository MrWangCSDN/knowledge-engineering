"""agent 自由输出不变量：prompt 不再要求 6 段 JSON；含渲染工具指引。

房规：prompt 是大段字符串/嵌进 SSE 闭包，不易行为单测 → 用源码不变量兜底
（沿用本仓库 chat.test.ts / contextbar 手法）。
"""
from pathlib import Path

_RS = Path("src/service/qa_engine/react_synthesizer.py").read_text(encoding="utf-8")
_PR = Path("src/service/qa_engine/prompts.py").read_text(encoding="utf-8")


def test_tool_hint_no_six_section_json():
    # 截取 _build_tool_usage_hint 函数体
    i = _RS.index("def _build_tool_usage_hint")
    j = _RS.index("def _tools_to_openai_schema", i)
    body = _RS[i:j]
    assert "6 段" not in body and "6段" not in body        # 不再要求 6 段
    assert "render_call_graph" in body                      # 含渲染工具指引


def test_agent_system_prompt_free_markdown_and_render_tool():
    # 定位赋值语句（非模块 docstring 里的提及）→ 精确截取三引号字符串本体
    i = _PR.index('AGENT_SYSTEM_PROMPT = """')
    open_q = _PR.index('"""', i)              # 开头三引号
    close_q = _PR.index('"""', open_q + 3)    # 结尾三引号
    body = _PR[open_q + 3:close_q]
    assert "6 段" not in body and "6段" not in body          # prompt 体不含 6 段强制
    # 自由 markdown 取向（任一关键词命中即可）
    assert ("自由" in body) or ("markdown" in body.lower()) or ("自然" in body)
    # 调用图优先走 render_call_graph 工具（让确定性渲染工具真正被用上）
    assert "render_call_graph" in body
