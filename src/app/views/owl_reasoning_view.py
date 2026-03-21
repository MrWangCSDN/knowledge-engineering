"""OWL 推理：补全间接依赖、补全前后对比、补全效果一览。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import streamlit as st

from src.app.ui.streamlit_keys import SessionKeys


class OwlReasoningView:
    """智能推理步骤：补全、对比、效果一览。"""

    def __init__(
        self,
        graph: Optional[Any],
        neo4j_fallback: Optional[Any],
        root: Path,
    ):
        self._graph = graph
        self._neo4j = neo4j_fallback
        self._root = root

    def render(self) -> None:
        """渲染 OWL 推理完整流程。"""
        owl_data_source = self._graph if self._graph is not None else self._neo4j
        if owl_data_source is None:
            st.warning("请先完成流水线或配置 Neo4j，再使用依赖补全。")
            return

        try:
            from src.knowledge.ontology import (
                run_ontology_pipeline,
                TRANSITIVE_REL_TYPES,
            )
            has_owl = True
        except ImportError:
            has_owl = False
            st.warning('未安装依赖补全组件。请在项目根目录执行：`pip install -e ".[owl]"`')
            return

        ds_label = "内存图" if self._graph is not None else "Neo4j"
        st.caption(f"当前数据源：**{ds_label}**")

        st.markdown("#### 试试看：补全前后影响范围对比")
        st.caption(
            "输入实体 ID，或在下方「实体级对比」卡片中点「用此实体对比」，即可看到该实体的完整对比。"
        )
        if self._graph is not None:
            self._render_memory_compare()
        if self._neo4j is not None and self._graph is None:
            self._render_neo4j_compare()

        st.divider()
        with st.expander("补全规则涉及的关系类型", expanded=False):
            st.caption(
                "以下关系类型会被当作「可传递」处理：若图中已有 A→B、B→C，则补全时自动推断 A→C。"
                "代码层面：calls（调用）、extends（继承）、implements（实现）、depends_on（依赖）、belongs_to（归属）；"
                "服务层面：service_calls（服务调用）；"
                "业务层面：BELONGS_TO_DOMAIN（归属业务域）、CONTAINS_CAPABILITY（业务域包含能力）。"
            )
            st.code(", ".join(sorted(TRANSITIVE_REL_TYPES)), language=None)
        st.markdown("#### 开始补全")
        col1, col2 = st.columns(2)
        with col1:
            export_owl = st.checkbox(
                "导出推理规则到文件（Turtle）", value=True, key=SessionKeys.OWL_EXPORT
            )
            write_inferred = st.checkbox(
                "把补全结果写回图谱",
                value=True,
                key=SessionKeys.OWL_WRITE_BACK,
                help="写回后，④ 评估改动影响 中的影响范围会更完整",
            )
        with col2:
            st.radio(
                "推理引擎",
                ["builtin"],
                format_func=lambda x: "内置",
                key=SessionKeys.OWL_REASONER,
                disabled=True,
            )
            st.caption("当前使用内置补全引擎。")

        if st.button("开始补全", type="primary", key=SessionKeys.OWL_RUN):
            out_dir = Path("out")
            export_path = out_dir / "knowledge_ontology.ttl" if export_owl else None
            if export_path and export_owl:
                out_dir.mkdir(parents=True, exist_ok=True)
            with st.spinner("正在补全间接依赖…"):
                result = run_ontology_pipeline(
                    owl_data_source,
                    export_owl=export_owl,
                    export_path=export_path,
                    run_reasoner="builtin",
                    write_inferred_to_graph=write_inferred,
                )
            st.session_state[SessionKeys.OWL_LAST_RESULT] = result
            st.rerun()

        result = st.session_state.get(SessionKeys.OWL_LAST_RESULT)
        if result:
            if result.get("errors"):
                for err in result["errors"]:
                    st.error(err)
            else:
                st.success("补全已完成")
                c1, c2, c3 = st.columns(3)
                c1.metric("新增边数", result.get("inferred_count", 0))
                c2.metric("已写回图谱", result.get("written_to_graph", 0))
                c3.metric("规则导出", "已导出" if result.get("export_path") else "未导出")
                if result.get("written_to_graph", 0) > 0:
                    st.caption(
                        "补全结果已写回图谱，影响分析中的下游/上游范围会更完整。"
                        "下方 **补全效果一览** 可查看边数变化与受益实体。"
                    )
                if result.get("export_path"):
                    st.caption(f"规则文件：`{result['export_path']}`")
                    try:
                        ttl_content = Path(result["export_path"]).read_text(encoding="utf-8")
                        with st.expander("预览规则文件（Turtle）", expanded=False):
                            st.text(ttl_content[:8000] + ("…" if len(ttl_content) > 8000 else ""))
                    except Exception:
                        pass

        # 补全效果一览：重计算（遍历推断边 + 影响闭包），默认不触发，避免切 Step4 卡顿。
        # 仅在「刚执行过补全且写回成功」或用户手动加载时才计算。
        auto_load = bool(result and not result.get("errors") and result.get("written_to_graph", 0) > 0)
        if auto_load:
            st.session_state[SessionKeys.OWL_BENEFIT_LOADED] = True

        if st.session_state.get(SessionKeys.OWL_BENEFIT_LOADED) or auto_load:
            with st.expander("补全效果一览（已加载）", expanded=True):
                self._render_benefit_overview()
        else:
            st.caption("尚未加载补全效果一览（该区域会做较重的影响范围计算）。")
            if st.button("加载补全效果一览", key=SessionKeys.OWL_BENEFIT_LOAD_BTN, type="secondary"):
                st.session_state[SessionKeys.OWL_BENEFIT_LOADED] = True
                st.rerun()

    def _render_memory_compare(self) -> None:
        owl_do_compare = st.session_state.pop(SessionKeys.OWL_DO_COMPARE, None)
        prefill = st.session_state.pop(SessionKeys.OWL_COMPARE_PREFILL, None)
        if prefill is not None:
            st.session_state[SessionKeys.OWL_COMPARE_ENTITY_ID] = prefill
        owl_entity_id = st.text_input(
            "实体 ID",
            placeholder="输入 ID，或从下方对比卡片点「用此实体对比」自动填入",
            key=SessionKeys.OWL_COMPARE_ENTITY_ID,
        )
        owl_direction = st.radio(
            "方向",
            ["down", "up"],
            format_func=lambda x: "下游（被谁调用/依赖）" if x == "down" else "上游（依赖了谁）",
            key=SessionKeys.OWL_COMPARE_DIR,
        )
        run_compare = st.button("对比影响范围", key=SessionKeys.OWL_COMPARE_BTN) or (
            owl_do_compare and owl_entity_id and owl_entity_id.strip()
        )
        if run_compare and owl_entity_id and owl_entity_id.strip():
            eid = owl_entity_id.strip()
            if not self._graph._g.has_node(eid):
                st.warning(f"图中不存在节点：{eid}")
            else:
                only_explicit = self._graph.impact_closure(
                    eid, direction=owl_direction, max_depth=50, exclude_inferred=True
                )
                with_inferred = self._graph.impact_closure(
                    eid, direction=owl_direction, max_depth=50, exclude_inferred=False
                )
                extra = with_inferred - only_explicit
                st.success("对比结果（本页展示，无需切换步骤）")
                c1, c2, c3 = st.columns(3)
                c1.metric("仅显式边可达节点数", len(only_explicit))
                c2.metric("含推断边可达节点数", len(with_inferred))
                c3.metric("因推理新增的可达节点数", len(extra))
                if extra:
                    st.caption("**因推理而新增可达的节点**：")
                    for nid in sorted(extra)[:30]:
                        node = self._graph.get_node(nid)
                        name = (node or {}).get("name") or nid
                        st.code(f"{nid}  |  {name}")
                    if len(extra) > 30:
                        st.caption(f"… 共 **{len(extra)}** 个，仅展示前 30 个。")
                else:
                    st.info(
                        "该实体在补全前后可达范围相同。"
                        "请先点击下方「开始补全」并勾选「把补全结果写回图谱」，再试。"
                    )

    def _render_neo4j_compare(self) -> None:
        st.markdown("**查看某实体的影响范围**（Neo4j 数据源）")
        neo_entity_id = st.text_input(
            "实体 ID",
            placeholder="输入图中任意节点 ID",
            key=SessionKeys.OWL_NEO_ENTITY_ID,
        )
        neo_direction = st.radio(
            "方向",
            ["down", "up"],
            format_func=lambda x: "下游" if x == "down" else "上游",
            key=SessionKeys.OWL_NEO_DIR,
        )
        if st.button("在本页查看影响范围", key=SessionKeys.OWL_NEO_BTN) and neo_entity_id and neo_entity_id.strip():
            eid = neo_entity_id.strip()
            if not self._neo4j.has_node(eid):
                st.warning(f"图中不存在节点：{eid}")
            else:
                closure = self._neo4j.impact_closure(
                    eid, direction=neo_direction, max_depth=50
                )
                nodes = [
                    self._neo4j.get_node(nid)
                    for nid in closure
                    if self._neo4j.get_node(nid)
                ]
                st.success(f"共 **{len(closure)}** 个可达节点（本页展示）")
                for n in nodes[:50]:
                    st.code(f"{n.get('id')}  |  {n.get('name') or n.get('entity_type') or ''}")
                if len(nodes) > 50:
                    st.caption(f"仅展示前 50 个，共 {len(nodes)} 个。")

    def _render_benefit_overview(self) -> None:
        inferred_in_graph = []
        if self._graph is not None:
            for u, v, rel_type, attrs in self._graph.iter_edges():
                if attrs.get("inferred"):
                    inferred_in_graph.append({"源": u, "目标": v, "关系": rel_type})
        elif self._neo4j is not None:
            raw = self._neo4j.list_inferred_edges(limit=500)
            inferred_in_graph = [
                {
                    "源": x.get("source"),
                    "目标": x.get("target"),
                    "关系": x.get("rel_type") or "",
                }
                for x in raw
            ]
        if not inferred_in_graph:
            return

        st.divider()
        st.markdown("#### 补全效果一览")
        involved = set()
        for e in inferred_in_graph:
            s, t = e.get("源"), e.get("目标")
            if s:
                involved.add(s)
            if t:
                involved.add(t)
        n_inferred = len(inferred_in_graph)

        if self._graph is not None:
            total_edges = self._graph.edge_count()
            explicit_only = total_edges - n_inferred
            st.markdown("**图中边数**")
            before_col, arrow_col, after_col = st.columns([1, 0.4, 1])
            with before_col:
                st.metric("补全前", explicit_only, help="仅直接依赖时的边数")
            with arrow_col:
                st.markdown(
                    f"<br><br>→ **+{n_inferred}** 条补全边 →",
                    unsafe_allow_html=True,
                )
            with after_col:
                st.metric("补全后", total_edges, help="当前图谱总边数")
            st.caption("边数增加后，做影响分析时能沿更多边遍历，下游/上游范围更大。")
            st.markdown("")

        if self._graph is not None and n_inferred > 0:
            sources = list(
                set(
                    e.get("源")
                    for e in inferred_in_graph
                    if e.get("源") and self._graph._g.has_node(e.get("源"))
                )
            )[:25]
            benefit = []
            for eid in sources:
                only_ex = self._graph.impact_closure(
                    eid, direction="down", max_depth=50, exclude_inferred=True
                )
                with_inf = self._graph.impact_closure(
                    eid, direction="down", max_depth=50, exclude_inferred=False
                )
                extra_ids = sorted(with_inf - only_ex)
                delta = len(extra_ids)
                if delta <= 0:
                    continue
                node = self._graph.get_node(eid)
                name = (node or {}).get("name") or eid
                display_name = (
                    (name[:20] + "…") if name and len(str(name)) > 20 else (name or eid)
                )
                benefit.append(
                    {
                        "实体": display_name,
                        "eid": eid,
                        "仅显式可达": len(only_ex),
                        "含推断可达": len(with_inf),
                        "新增": delta,
                        "extra_ids": extra_ids,
                    }
                )
            benefit.sort(key=lambda x: x["新增"], reverse=True)
            benefit = benefit[:8]
            if benefit:
                st.markdown("**按实体：下游可达节点数（补全前 vs 补全后）**")
                try:
                    import pandas as pd
                    df_chart = pd.DataFrame(
                        {
                            "补全前": [b["仅显式可达"] for b in benefit],
                            "补全后": [b["含推断可达"] for b in benefit],
                        },
                        index=[b["实体"] for b in benefit],
                    )
                    st.bar_chart(df_chart, height=280)
                except Exception:
                    pass
                st.caption("柱高差异即补全带来的扩大效应。")
                st.markdown(
                    "**实体级对比**（可展开查看新增节点，或点「用此实体对比」在上方看完整对比）"
                )
                for i, b in enumerate(benefit):
                    with st.container():
                        c1, c2, c3, c4 = st.columns([2, 1, 1, 1.2])
                        with c1:
                            st.caption(f"**{b['实体']}**")
                        with c2:
                            st.metric("补全前", b["仅显式可达"], None)
                        with c3:
                            st.metric("补全后", b["含推断可达"], f"+{b['新增']}")
                        with c4:
                            if st.button(
                                "用此实体对比",
                                key=SessionKeys.owl_use_entity_button(i),
                                type="secondary",
                            ):
                                st.session_state[SessionKeys.OWL_COMPARE_PREFILL] = b["eid"]
                                st.session_state[SessionKeys.OWL_DO_COMPARE] = True
                                st.rerun()
                        with st.expander(f"因补全新增的可达节点（共 {b['新增']} 个）"):
                            for nid in b["extra_ids"][:40]:
                                node = self._graph.get_node(nid)
                                name = (node or {}).get("name") or nid
                                st.caption(f"{nid}  |  {name}")
                            if len(b["extra_ids"]) > 40:
                                st.caption(f"… 共 {len(b['extra_ids'])} 个，仅展示前 40 个。")
                st.caption("以上为下游影响范围扩大较多的实体，数据来自当前图谱。")
