"""sse_emitter 后端兜底：剥 agent 误把 render_call_graph 工具参数写成 markdown 代码块。

实测背景（2026-06-08）：LLM 偶尔 narrate-tool 退化——把 render_call_graph 的 entity_id /
direction / depth 当 ```render_call_graph\\n...\\n``` 代码块写进答案正文，导致前端
显示一段"看着像 YAML"的工具参数代码块、图根本没渲染。

修：sse_emitter 在 dump 前扫每个 section.content，正则剥这种代码块。
"""
from src.service.qa_engine.sse_emitter import _strip_render_call_graph_codeblock


def test_strip_render_call_graph_yaml_form():
    """剥 ```render_call_graph 包裹的 YAML-like 工具参数代码块（实测案例）。"""
    content = (
        "见下方核心调用图：\n\n"
        "```render_call_graph\n"
        "entity_id: method://OrderTimeOutCancelTask::cancelTimeOutOrder\n"
        "direction: down\n"
        "depth: 3\n"
        "```\n\n"
        "## 实现机制一：定时任务批量扫描\n"
        "..."
    )
    out = _strip_render_call_graph_codeblock(content)
    # 整块 fenced code block 已剥（含 ``` 起止 + 内容）
    assert "```render_call_graph" not in out
    assert "entity_id: method://OrderTimeOutCancelTask" not in out
    # 周围文字保留
    assert "见下方核心调用图" in out
    assert "## 实现机制一：定时任务批量扫描" in out


def test_strip_render_call_graph_json_form():
    """另一种常见格式：JSON 形式的 tool args。"""
    content = (
        "调用图：\n"
        '```render_call_graph\n'
        '{"entity_id": "method://Cls::m", "direction": "down"}\n'
        "```\n"
        "其它内容"
    )
    out = _strip_render_call_graph_codeblock(content)
    assert "```render_call_graph" not in out
    assert '{"entity_id"' not in out
    assert "其它内容" in out


def test_strip_preserves_other_code_blocks():
    """剥时不误伤 java/python 等真实代码块。"""
    content = (
        "```java\n"
        "@Scheduled(cron = \"0 0/10 * ? * ?\")\n"
        "private void cancelTimeOutOrder(){}\n"
        "```\n\n"
        "```render_call_graph\n"
        "entity_id: method://X::y\n"
        "```\n\n"
        "末尾"
    )
    out = _strip_render_call_graph_codeblock(content)
    # java 代码块保留
    assert "```java" in out
    assert "@Scheduled" in out
    # render_call_graph 代码块剥掉
    assert "```render_call_graph" not in out
    assert "entity_id: method://X::y" not in out
    # 末尾文字保留
    assert "末尾" in out


def test_strip_handles_multiple_occurrences():
    """多个 render_call_graph 代码块都剥（防 agent 写多次）。"""
    content = (
        "```render_call_graph\n"
        "entity_id: A\n"
        "```\n"
        "中间文字\n"
        "```render_call_graph\n"
        "entity_id: B\n"
        "```\n"
        "尾"
    )
    out = _strip_render_call_graph_codeblock(content)
    assert "```render_call_graph" not in out
    assert "entity_id: A" not in out
    assert "entity_id: B" not in out
    assert "中间文字" in out
    assert "尾" in out


def test_strip_noop_when_no_codeblock():
    """无 render_call_graph 代码块时原样返回（不影响正常 content）。"""
    content = "正常答案内容\n\n## 标题\n\n```java\nint x = 1;\n```\n结尾"
    out = _strip_render_call_graph_codeblock(content)
    assert out == content


def test_strip_empty_or_none():
    """空字符串 / None 不崩。"""
    assert _strip_render_call_graph_codeblock("") == ""
    assert _strip_render_call_graph_codeblock(None) is None
