"""渲染类工具结果只把 summary 回灌 LLM；render.data 不进 LLM 上下文。"""
from src.service.qa_engine.react_synthesizer import _tool_message_content


def test_render_result_feeds_only_summary():
    out = {"render": {"kind": "call_graph", "data": {"nodes": [1, 2, 3], "edges": []}}, "summary": "已渲染 X（3 节点）"}
    content = _tool_message_content(out)
    assert "已渲染 X" in content          # summary 回灌
    assert "nodes" not in content          # 图数据不灌（省 token、防复述）


def test_non_render_result_feeds_full_json():
    out = {"entity_id": "A::m#()", "callees": ["B::n#()"]}
    content = _tool_message_content(out)
    assert "callees" in content            # 调查类工具结果照常全量回灌


def test_render_none_feeds_full_json():
    # render=None（无图）→ 当普通结果全量回灌（含 summary/error 供 LLM 判断）
    out = {"render": None, "summary": "未找到调用关系"}
    content = _tool_message_content(out)
    assert "未找到调用关系" in content
