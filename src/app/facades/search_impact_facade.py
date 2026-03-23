from __future__ import annotations

from typing import Callable, Any

import streamlit as st

from src.app.ui.streamlit_keys import SessionKeys

from src.app.components.ontology_browser import OntologyBrowser
from src.app.views.search_impact_view import SearchImpactView
from src.app.utils.node_utils import format_node_display_label


class SearchImpactFacade:
    """步骤 ③：理解具体代码 & 评估改动影响（组合 SearchImpactView + OntologyBrowser）。"""

    def __init__(
        self,
        *,
        get_data_backend: Callable[[], tuple[Any, Any]],
        get_weaviate_svc: Callable[[], Any],
        format_node_label: Callable[[Any], str] = format_node_display_label,
        get_neo4j_backend_optional: Callable[[], Any] | None = None,
    ):
        self._get_data_backend = get_data_backend
        self._get_weaviate_svc = get_weaviate_svc
        self._format_node_label = format_node_label
        self._get_neo4j_backend_optional = get_neo4j_backend_optional

    def render_step3(self, *, neo4j_fallback: Any) -> None:
        st.subheader("③ 理解具体代码 & 评估改动影响")
        with st.container():
            search_view = SearchImpactView(
                get_data_backend=self._get_data_backend,
                weaviate_svc=self._get_weaviate_svc(),
                format_node_label=self._format_node_label,
            )
            selected_id = search_view.render_search_section()
            if selected_id:
                search_view.render_selected_entity_and_subgraph(selected_id)

            st.divider()
            st.markdown("<div style='margin-top:1.2rem;'></div>", unsafe_allow_html=True)
            st.markdown("##### 按实体类型浏览（包 / 服务 / API 端点 / 类·方法等 A–Z）")
            st.caption(
                "按实体类型选择后：包与服务为下拉列表；API 端点为「模块 → 端点」二级联动；类、方法等为 A–Z 分区 + 区内下拉 + 关键词筛选。选中节点后展示属性与出边/入边。"
            )

            with st.expander("本体浏览（需要 Neo4j，懒加载）", expanded=False):
                neo4j_s3 = neo4j_fallback if neo4j_fallback is not None else (self._get_neo4j_backend_optional() if self._get_neo4j_backend_optional else None)
                close_s3 = neo4j_s3 is not None and neo4j_s3 is not neo4j_fallback
                if neo4j_s3 is None:
                    st.warning("未配置 Neo4j 或未启用 Neo4j 后端。按实体类型浏览需要 Neo4j，请在 `config/project.yaml` 中设置 `knowledge.graph.backend: neo4j` 并运行流水线完成图谱同步。")
                else:
                    try:
                        def _on_use_for_impact(nid: str) -> None:
                            st.session_state[SessionKeys.SELECTED_ENTITY_ID] = nid
                            st.session_state[SessionKeys.IMPACT_ID] = nid

                        browser = OntologyBrowser(
                            neo4j_s3,
                            "step3_ontology",
                            self._get_weaviate_svc(),
                            show_use_for_impact_button=True,
                            on_use_for_impact=_on_use_for_impact,
                            wrap_interpretation_in_expander=False,
                        )
                        browser.render()
                    finally:
                        if close_s3 and neo4j_s3 is not None:
                            neo4j_s3.close()

            st.divider()
            search_view.render_impact_section()

