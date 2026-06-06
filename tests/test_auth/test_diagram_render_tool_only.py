"""画图唯一规则：agent 路径所有节点-边图一律走 render_call_graph 工具，禁止手画。

背景：线上实测 agent 对"用户提交订单怎么生成订单"手画了非法 Mermaid（前端解析失败），
根因是 prompt 自相矛盾——既要求调 render_call_graph 工具，又（L180/L276/A1块）教/要求
手画 Mermaid/reactflow 调用图。本次收敛为单一规则。6段（free_format=False）路径不动。
"""
from src.service.qa_engine.prompts import AGENT_SYSTEM_PROMPT, build_user_prompt, _SKILL_HINTS


def _ctx():
    """带调用边 + 2b 解读的最小 context（触发 build_user_prompt 的调用链/画图分支）。"""
    return {
        "entry_candidates": [{"entity_id": "A::m#()", "level": "method", "summary_text": "业务X"}],
        "call_edges_by_entry": {"A::m#()": [("A::m#()", "B::n#()")]},
        "callchain_node_summaries": {"A::m#()": "业务X"},
        "skill_id": "architecture",
    }


def test_agent_system_prompt_only_tool_no_handdraw():
    """AGENT_SYSTEM_PROMPT：只让调 render_call_graph，明确禁手画，且不再含 mermaid/reactflow 代码块教程。"""
    body = AGENT_SYSTEM_PROMPT
    assert "render_call_graph" in body                       # 指向工具
    assert "手画" in body and "严禁" in body                  # 明确禁手画
    assert "```mermaid" not in body                          # 不再教 mermaid fence
    assert "```reactflow" not in body                        # 不再教 reactflow fence
    assert "按下方 Mermaid 约定画图" not in body              # 作答风格不再指向 Mermaid


def test_free_format_user_prompt_points_to_tool_not_handdraw():
    """free_format（agent）user prompt：调用图指向 render_call_graph，不出 A1 手画 call_chain 指令。"""
    p = build_user_prompt("q", _ctx(), free_format=True)
    assert "render_call_graph" in p                          # 指向工具
    assert "A1 锚定式" not in p                               # 不给手画业务流程图指令
    assert "画 call_chain 业务流程图时" not in p


def test_six_section_user_prompt_unchanged():
    """6段（free_format=False）路径回归保护：仍给 call_chain 画图 + A1 锚定指令。"""
    p = build_user_prompt("q", _ctx(), free_format=False)
    assert "A1 锚定式" in p                                   # 6段仍给业务流程图锚定指令
    assert "render_call_graph" not in p                      # 6段不调工具（一次性合成）


def test_dependency_skill_hint_mechanism_neutral():
    """dependency skill hint 去掉 Mermaid 字样（机制由各路径 system prompt 决定）。"""
    assert "Mermaid" not in _SKILL_HINTS["dependency"]
    assert "调用图" in _SKILL_HINTS["dependency"]             # 仍要求出调用图


def test_agent_prompt_forbids_sequential_section_numbering():
    """禁止 一/二/三 顺序编号小节——避免这次没出调用图(没有"一、")时出现孤儿"二、"。"""
    body = AGENT_SYSTEM_PROMPT
    assert ("不要给小节编号" in body) or ("描述性小标题" in body)


def test_agent_prompt_forbids_markdown_image_placeholder_for_graphs():
    """禁止用 markdown 图片语法 ![]() 或'已渲染'旁白来引用图——图由工具自动内联，只说'见下方调用图'。

    背景：实测 agent 写出 `![paySuccess调用图](render_call_graph 工具已渲染)`（裂图）+ '调用图已渲染'旁白。
    """
    body = AGENT_SYSTEM_PROMPT
    assert "图片语法" in body          # 规则里点名禁止 markdown 图片语法
    assert "![" in body               # 出现 ![ 这个被禁的语法样例


def test_agent_prompt_no_a2_auto_shown():
    """A2 撤销：回归 agent-native 后，AGENT_SYSTEM_PROMPT 不再声称'主图已自动展示'。"""
    # A2 当时让 agent'以为'系统已出图（说'如上图所示'）→ 现在 agent 自己调工具画图，撤掉这句
    assert "自动展示" not in AGENT_SYSTEM_PROMPT


def test_free_format_user_prompt_no_a2_auto_shown():
    """A2 撤销：free_format user prompt 也不再声称'主图已自动展示'。"""
    from src.service.qa_engine.prompts import build_user_prompt
    # 构造带 entry_candidates 的 ctx（走画图指令分支），确认其中不含 A2 的'自动展示'话术
    p = build_user_prompt("q", {"entry_candidates": [], "skill_id": "architecture"}, free_format=True)
    assert "自动展示" not in p


def test_agent_prompt_has_two_draw_modes():
    """唯一出口双模式：AGENT_SYSTEM_PROMPT 教 entity_id（代码调用图）+ nodes/edges（逻辑/架构图）两种用法。"""
    body = AGENT_SYSTEM_PROMPT
    assert "render_call_graph" in body
    assert "entity_id" in body                          # 模式 A：代码调用图
    assert "nodes" in body and "edges" in body          # 模式 B：freeform 逻辑/架构图
    assert "严禁" in body and "手画" in body             # 仍禁手画（唯一出口）


def test_render_call_graph_desc_mentions_two_modes():
    """工具描述告知双模式（entity_id 调用图 / nodes-edges 逻辑图）。"""
    from src.service.qa_engine.tools.render_call_graph import build_render_call_graph_tool

    class _G:
        def successors(self, n):
            return []

        def predecessors(self, n):
            return []

    desc = build_render_call_graph_tool(_G()).description
    assert "nodes" in desc or "逻辑" in desc             # 描述提到 freeform/逻辑图能力
