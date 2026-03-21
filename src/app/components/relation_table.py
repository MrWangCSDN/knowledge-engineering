"""关系表格与节点详情面板组件。"""
from __future__ import annotations

import html
import json
from typing import Any

import streamlit as st

from src.app.ui.streamlit_keys import SessionKeys


class RelationTableWithDetail:
    """渲染关系表格：保留原有全部列，最后一列增加「查看详情」。"""

    REL_TYPE_KEY = "rel_type"

    @staticmethod
    def render(
        rows: list[dict],
        id_key: str,
        name_key: str,
        type_key: str,
        key_prefix: str,
        neo4j_backend: Any,
        *,
        id_label: str = "ID",
        name_label: str = "名称",
        type_label: str = "类型",
    ) -> None:
        rel_type_key = RelationTableWithDetail.REL_TYPE_KEY
        c1, c2, c3, c4, c5 = st.columns([1, 3, 2, 2, 1])
        c1.caption("**关系类型**")
        c2.caption(f"**{id_label}**")
        c3.caption(f"**{name_label}**")
        c4.caption(f"**{type_label}**")
        c5.caption("**操作**")
        st.divider()
        for i, row in enumerate(rows):
            nid = (row.get(id_key) or "").strip()
            rtype = row.get(rel_type_key) or "—"
            name = row.get(name_key) or "—"
            etype = row.get(type_key) or "—"
            col1, col2, col3, col4, col5 = st.columns([1, 3, 2, 2, 1])
            with col1:
                st.caption(rtype)
            with col2:
                st.text(nid or "—")
            with col3:
                st.text(name)
            with col4:
                st.caption(etype)
            with col5:
                if st.button(
                    "查看详情",
                    key=SessionKeys.relation_row_button(key_prefix, i),
                    type="secondary",
                ):
                    st.session_state[SessionKeys.ONTOLOGY_DETAIL_NID] = nid
                    st.session_state[SessionKeys.ONTOLOGY_DETAIL_KEY_PREFIX] = key_prefix
                    st.rerun()
            if (
                st.session_state.get(SessionKeys.ONTOLOGY_DETAIL_NID) == nid
                and st.session_state.get(SessionKeys.ONTOLOGY_DETAIL_KEY_PREFIX) == key_prefix
            ):
                node = neo4j_backend.get_node(nid)
                rels_detail = neo4j_backend.get_node_relations(nid)
                if st.button("关闭详情", key=SessionKeys.ontology_close_button(key_prefix)):
                    st.session_state.pop(SessionKeys.ONTOLOGY_DETAIL_NID, None)
                    st.session_state.pop(SessionKeys.ONTOLOGY_DETAIL_KEY_PREFIX, None)
                    st.rerun()
                inner_parts = []
                if node:
                    for k, v in [
                        ("ID", node.get("id") or nid),
                        ("名称", node.get("name") or "—"),
                        ("实体类型", node.get("entity_type") or "—"),
                        ("位置", node.get("location") or "—"),
                        ("模块", node.get("module_id") or "—"),
                    ]:
                        inner_parts.append(
                            f"<p style='margin:4px 0;'><b>{html.escape(k)}:</b> {html.escape(str(v))}</p>"
                        )
                    other = {
                        k: v
                        for k, v in node.items()
                        if k not in ("id", "name", "entity_type", "location", "module_id")
                        and v is not None
                    }
                    if other:
                        inner_parts.append(
                            "<p><b>其他属性</b></p><pre style='background:#fff; padding:8px; border-radius:4px; font-size:12px;'>"
                            + html.escape(json.dumps(other, ensure_ascii=False, indent=2))
                            + "</pre>"
                        )
                else:
                    inner_parts.append("<p style='color:#888;'>未在 Neo4j 中查到该节点。</p>")
                inner_parts.append("<p><b>出边</b></p>")
                if rels_detail["outgoing"]:
                    rows_html = "".join(
                        f"<tr><td>{html.escape(str(r.get('rel_type') or ''))}</td><td>{html.escape(str(r.get('target_name') or ''))}</td><td>{html.escape(str(r.get('target_type') or ''))}</td></tr>"
                        for r in rels_detail["outgoing"]
                    )
                    inner_parts.append(
                        "<table style='width:100%; border-collapse:collapse; font-size:13px;'>"
                        "<thead><tr><th style='text-align:left; border-bottom:1px solid #ccc;'>关系</th>"
                        "<th style='text-align:left; border-bottom:1px solid #ccc;'>目标</th>"
                        "<th style='text-align:left; border-bottom:1px solid #ccc;'>类型</th></tr></thead>"
                        f"<tbody>{rows_html}</tbody></table>"
                    )
                else:
                    inner_parts.append("<p style='color:#666;'>无</p>")
                inner_parts.append("<p><b>入边</b></p>")
                if rels_detail["incoming"]:
                    rows_html = "".join(
                        f"<tr><td>{html.escape(str(r.get('rel_type') or ''))}</td><td>{html.escape(str(r.get('source_name') or ''))}</td><td>{html.escape(str(r.get('source_type') or ''))}</td></tr>"
                        for r in rels_detail["incoming"]
                    )
                    inner_parts.append(
                        "<table style='width:100%; border-collapse:collapse; font-size:13px;'>"
                        "<thead><tr><th style='text-align:left; border-bottom:1px solid #ccc;'>关系</th>"
                        "<th style='text-align:left; border-bottom:1px solid #ccc;'>来源</th>"
                        "<th style='text-align:left; border-bottom:1px solid #ccc;'>类型</th></tr></thead>"
                        f"<tbody>{rows_html}</tbody></table>"
                    )
                else:
                    inner_parts.append("<p style='color:#666;'>无</p>")
                panel_html = (
                    "<div style='background:#f5f5f5; border:1px solid #bbb; border-radius:8px; padding:16px; margin:12px 0; "
                    "box-shadow:0 1px 3px rgba(0,0,0,0.08);'>"
                    "<p style='margin-top:0; font-weight:600;'>📌 节点详情</p>"
                    + "".join(inner_parts)
                    + "</div>"
                )
                st.markdown(panel_html, unsafe_allow_html=True)
            if i < len(rows) - 1:
                st.divider()
