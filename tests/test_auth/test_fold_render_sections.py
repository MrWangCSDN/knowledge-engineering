"""fold_render_sections：把 agent 自由输出里调的调用图，按 at 偏移折叠进 sections。

治根因：完成态前端按 sections 顺序渲染——只要图作为 call_chain 段落在正文对应位置，
就既持久化（reopen 不丢）又有序（不跳末尾）。设计 [[业务问答-reactflow御用画图工具-设计]] §四④①+②。

折叠出的段标 headerless=True：agent 自由输出是一段连续叙述、不该出现「📌 回答」小节头
（原本单段靠 sections.length===1 免头，折成多段后失效，故显式标记，前端据此免头）。
"""
import json

from src.service.qa_engine.sse_emitter import fold_render_sections


def test_fold_inserts_call_chain_by_at():
    """单 overview 文本 + 一张图(at=5) → [文本段, call_chain 段, 文本段]，图在正文对应位置。"""
    sections = [{"type": "overview", "title": "回答", "content": "前文AAA中段BBB后文"}]
    renders = [{"at": 5, "data": {"nodes": [{"id": "x"}], "edges": []}}]
    out = fold_render_sections(sections, renders)
    assert [s["type"] for s in out] == ["overview", "call_chain", "overview"]
    # at=5 切在 "前文AAA"(5字) 之后
    assert out[0]["content"] == "前文AAA"
    assert out[2]["content"] == "中段BBB后文"
    # call_chain 段 content = {nodes,edges} JSON 字符串（与 6 段 call_chain 同构，前端 tryParseCallChain 吃）
    assert out[1]["content"] == json.dumps({"nodes": [{"id": "x"}], "edges": []}, ensure_ascii=False)
    # 折叠段都 headerless（agent 自由输出无小节头）
    assert all(s.get("headerless") for s in out)


def test_fold_at_zero_graph_first():
    """at=0 → 图在最前（正文段紧随其后）。"""
    sections = [{"type": "overview", "content": "正文"}]
    out = fold_render_sections(sections, [{"at": 0, "data": {"nodes": [], "edges": []}}])
    assert out[0]["type"] == "call_chain"
    assert out[1]["content"] == "正文"


def test_fold_two_graphs_ordered_by_at():
    """两张图按 at 升序插入，正文被切成对应段。"""
    sections = [{"type": "overview", "content": "AABBCC"}]  # 6 字
    renders = [{"at": 4, "data": {"nodes": [{"id": "g2"}], "edges": []}},
               {"at": 2, "data": {"nodes": [{"id": "g1"}], "edges": []}}]  # 故意乱序
    out = fold_render_sections(sections, renders)
    # 期望：AA | g1 | BB | g2 | CC
    assert [s["type"] for s in out] == ["overview", "call_chain", "overview", "call_chain", "overview"]
    assert out[0]["content"] == "AA" and out[2]["content"] == "BB" and out[4]["content"] == "CC"
    assert '"g1"' in out[1]["content"] and '"g2"' in out[3]["content"]


def test_fold_no_renders_unchanged():
    """无图 → 原样返回（单段叙述不动）。"""
    sections = [{"type": "overview", "content": "无图"}]
    assert fold_render_sections(sections, []) == sections


def test_fold_empty_text_chunks_dropped():
    """图前/后文本为空 → 不产生空文本段。"""
    sections = [{"type": "overview", "content": "X"}]
    out = fold_render_sections(sections, [{"at": 1, "data": {"nodes": [], "edges": []}}])  # at=1=末尾
    # "X" 在图前，图后为空 → [文本(X), call_chain]，无尾随空段
    assert [s["type"] for s in out] == ["overview", "call_chain"]


def test_fold_failsoft_on_bad_input():
    """异常输入（renders 非预期结构）→ fail-soft 返回原 sections，不抛、不阻断持久化。"""
    sections = [{"type": "overview", "content": "正文"}]
    out = fold_render_sections(sections, [{"at": "bad", "data": None}])
    assert out == sections
