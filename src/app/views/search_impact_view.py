"""检索与影响分析视图。"""
from __future__ import annotations

from typing import Any, Callable, Optional

import streamlit as st

from src.app.ui.streamlit_keys import SessionKeys


class SearchImpactView:
    """按名称检索 + 评估改动影响（含推理增强）。"""

    def __init__(
        self,
        get_data_backend: Callable[[], tuple[Any, Any]],
        weaviate_svc: Any,
        format_node_label: Callable[[dict], str],
    ):
        self._get_backend = get_data_backend
        self._weaviate_svc = weaviate_svc
        self._format_label = format_node_label

    def render_search_section(self) -> Optional[str]:
        """渲染按名称检索，返回 selected_entity_id。"""
        g, backend = self._get_backend()
        if g is None and backend is None:
            st.caption("请先运行流水线或配置 Neo4j 后再使用本步骤。")
            return None

        st.markdown("##### 按名称检索")
        col1, col2 = st.columns([2, 1])
        with col1:
            q = st.text_input(
                "关键词",
                placeholder="输入类名、方法名或业务概念…",
                key=SessionKeys.SEARCH_Q,
            )
        with col2:
            entity_type = st.selectbox(
                "实体类型",
                [
                    "",
                    "class",
                    "method",
                    "interface",
                    "Service",
                    "BusinessDomain",
                    "BusinessCapability",
                    "BusinessTerm",
                    "file",
                    "package",
                ],
                key=SessionKeys.SEARCH_TYPE,
            )
        selected_entity_id = None
        if q:
            types = [entity_type] if entity_type else None
            if g is not None:
                hits = g.search_by_name(q, entity_types=types)
            else:
                hits = backend.search_by_name(q, entity_types=types, limit=50)
            st.write(f"共 **{len(hits)}** 条结果")
            for hi, h in enumerate(hits[:50]):
                label = f"{h.get('name', h.get('id'))} ({h.get('entity_type', '')})"
                exp = st.expander(label)
                with exp:
                    st.json(h)
                    if st.button("作为当前实体进行分析", key=SessionKeys.search_use_hit_button(hi)):
                        st.session_state[SessionKeys.SELECTED_ENTITY_ID] = h.get("id")
                        st.session_state[SessionKeys.IMPACT_ID] = h.get("id")
                        selected_entity_id = h.get("id")
        if selected_entity_id is None:
            selected_entity_id = st.session_state.get(SessionKeys.SELECTED_ENTITY_ID)
        return selected_entity_id

    def render_selected_entity_and_subgraph(self, selected_entity_id: str) -> None:
        """渲染当前选中实体与服务/模块子图。"""
        g, backend = self._get_backend()
        if g is not None:
            node = g.get_node(selected_entity_id) or {}
        else:
            node = backend.get_node(selected_entity_id) or {}
        st.markdown("##### 3.2 当前选中实体")
        st.code(
            f"{node.get('id', selected_entity_id)} | {node.get('entity_type', '')} | {node.get('name', '')}"
        )
        st.markdown("##### 3.3 查看所在服务/模块的子图")
        module_id = node.get("module_id")
        service_id = f"service://{module_id}" if module_id else None
        if service_id:
            if g is not None:
                sub = g.subgraph_for_service(service_id)
            else:
                sub = backend.subgraph_for_service(service_id)
            st.write(f"服务/模块子图：节点 **{len(sub['nodes'])}**，边 **{len(sub['edges'])}**")
            st.json({"nodes": sub["nodes"][:30], "edges": sub["edges"][:50]})

    def render_impact_section(self) -> None:
        """渲染评估改动影响。"""
        g, backend = self._get_backend()
        if g is None and backend is None:
            st.caption("请先运行流水线或配置 Neo4j 后再使用。")
            return

        st.markdown("##### 评估改动影响（含推理增强）")
        st.caption("可手动输入或修改实体 ID。选择后即给出影响结果，无需额外点击。")
        if not st.session_state.get(SessionKeys.IMPACT_ID) and st.session_state.get(
            SessionKeys.SELECTED_ENTITY_ID
        ):
            st.session_state[SessionKeys.IMPACT_ID] = st.session_state[SessionKeys.SELECTED_ENTITY_ID]
        default_id = (
            st.session_state.get(SessionKeys.IMPACT_ID)
            or st.session_state.get(SessionKeys.SELECTED_ENTITY_ID)
            or ""
        )
        entity_id = st.text_input(
            "实体 ID",
            value=default_id,
            placeholder="如 class://xxx 或 method://xxx，上方检索/浏览选中后会自动填入",
            key=SessionKeys.IMPACT_ID,
        )
        direction = st.radio(
            "方向",
            ["down", "up"],
            format_func=lambda x: "下游（被谁调用/依赖）" if x == "down" else "上游（依赖了谁）",
            key=SessionKeys.IMPACT_DIR,
        )
        max_depth = st.slider("最大深度", 1, 100, 50, key=SessionKeys.IMPACT_DEPTH)
        mode_col1, mode_col2 = st.columns([1, 3])
        with mode_col1:
            impact_mode = st.selectbox(
                "模式",
                options=["explicit_only", "with_reasoning"],
                format_func=lambda v: "仅显式边" if v == "explicit_only" else "含推断边（推理增强）",
                key=SessionKeys.IMPACT_MODE,
            )
        with mode_col2:
            st.caption("含推断边模式会把补全写回的边也纳入影响范围，用于对比前后差异。")

        entity_id = (entity_id or "").strip()
        if not entity_id:
            st.info("请输入或在上方选择实体 ID 后，将自动计算影响范围并展示影响表。")
            return

        if g is not None:
            has_n = g._g.has_node(entity_id)
        else:
            has_n = backend.has_node(entity_id)
        if not has_n:
            st.warning(f"图中不存在节点: {entity_id}")
            return

        if g is not None:
            closure_explicit = g.impact_closure(
                entity_id, direction=direction, max_depth=max_depth, exclude_inferred=True
            )
            nodes_explicit = [g.get_node(nid) for nid in closure_explicit if g.get_node(nid)]
        else:
            closure_explicit = backend.impact_closure(
                entity_id, direction=direction, max_depth=max_depth
            )
            nodes_explicit = [
                backend.get_node(nid) for nid in closure_explicit if backend.get_node(nid)
            ]

        dir_label = "下游（被谁调用/依赖）" if direction == "down" else "上游（依赖了谁）"
        st.success(f"已计算影响范围：从当前实体沿 **{dir_label}** 共 **{len(closure_explicit)}** 个相关节点。")
        st.markdown("#### 影响表（仅显式边）")
        impact_rows = [
            {
                "节点 ID": n.get("id") or "",
                "类型": n.get("entity_type") or "",
                "名称": n.get("name") or "",
            }
            for n in nodes_explicit[:500]
        ]
        if impact_rows:
            st.dataframe(
                impact_rows,
                use_container_width=True,
                hide_index=True,
                height=min(400, 60 + len(impact_rows) * 38),
            )
        if len(nodes_explicit) > 500:
            st.caption(f"仅展示前 500 条，共 {len(nodes_explicit)} 个节点。")
        elif len(nodes_explicit) > 80:
            st.caption(f"共 **{len(nodes_explicit)}** 个节点。")

        if g is not None and impact_mode == "with_reasoning":
            has_inferred = any(
                attrs.get("inferred") for _, _, _, attrs in g.iter_edges()
            )
            if not has_inferred:
                st.info(
                    "当前图中尚无补全产生的边，上表为「仅显式边」影响范围。"
                    "若需推理增强对比，请先在 **④ 智能推理** 中点击「开始补全」并勾选「把补全结果写回图谱」，"
                    "再回到本页选择「含推断边」查看对比。"
                )
            else:
                closure_with = g.impact_closure(
                    entity_id, direction=direction, max_depth=max_depth, exclude_inferred=False
                )
                extra = set(closure_with) - set(closure_explicit)
                st.markdown("#### 推理增强对比：补全前后影响范围差异")
                c1, c2, c3 = st.columns(3)
                c1.metric("仅显式边可达节点数", len(closure_explicit))
                c2.metric("含推断边可达节点数", len(closure_with))
                c3.metric("因推理新增的可达节点数", len(extra))
                if extra:
                    st.caption("**因推理而新增纳入影响范围的节点**（下表为示例）：")
                    extra_rows = []
                    for nid in sorted(extra)[:100]:
                        node = g.get_node(nid)
                        extra_rows.append(
                            {
                                "节点 ID": nid,
                                "类型": (node or {}).get("entity_type") or "",
                                "名称": (node or {}).get("name") or nid,
                            }
                        )
                    if extra_rows:
                        st.dataframe(
                            extra_rows,
                            use_container_width=True,
                            hide_index=True,
                            height=min(300, 60 + len(extra_rows) * 38),
                        )
                    if len(extra) > 100:
                        st.caption(f"… 共 **{len(extra)}** 个，仅展示前 100 个。")
                else:
                    st.info(
                        "该实体在「仅显式边」与「含推断边」模式下的影响范围相同。"
                        "可能尚未执行推理，或该实体不涉及传递性关系。"
                    )
        elif backend is not None and impact_mode == "with_reasoning":
            st.caption(
                "**含推断边（推理增强）** 的对比需在 **④ 智能推理** 中执行补全并写回图谱后，"
                "使用内存图数据源查看。当前上表为按全部边计算的影响范围。"
            )
