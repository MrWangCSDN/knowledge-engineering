"""解读专区：按是否有数据动态展示「查看源码 / 技术解读 / 业务解读」Tab。"""
from __future__ import annotations

import html
from typing import TYPE_CHECKING, Optional

import streamlit as st

from src.core.domain_enums import INTERP_PANEL_ENTITY_TYPES, BusinessInterpretLevel

if TYPE_CHECKING:
    from src.app.services.weaviate_data_service import WeaviateDataService


class InterpretationPanel:
    """方法/类/接口的解读专区：源码、技术解读、业务解读。"""

    @staticmethod
    def render(
        entity_id: str,
        entity_type: str,
        node_detail: Optional[dict],
        weaviate_svc: "WeaviateDataService",
        *,
        wrap_in_expander: bool = True,
    ) -> None:
        """
        仅当有对应内容时才显示 Tab；三者皆无时整块「解读专区」不展示。
        entity_type 应为 method/class/interface 之一（小写）。
        """
        etype = (entity_type or "").lower()
        if etype not in INTERP_PANEL_ENTITY_TYPES:
            return

        nd = node_detail or {}
        trivial = etype == "method" and weaviate_svc.is_trivial_accessor_node(nd)

        code: Optional[str] = None
        if etype == "method":
            code = weaviate_svc.fetch_method_snippet(entity_id)
        has_source = bool(code and str(code).strip())

        inter: Optional[dict] = None
        if etype == "method" and not trivial:
            inter = weaviate_svc.fetch_method_interpretation(entity_id)
        has_tech = bool(inter and (inter.get("interpretation_text") or "").strip())

        biz: Optional[dict] = None
        if not trivial:
            level = "api" if etype == "method" else "class"
            biz = weaviate_svc.fetch_business_interpretation(entity_id, level=level)
        has_biz = bool(biz and (biz.get("summary_text") or "").strip())

        if not (has_source or has_tech or has_biz):
            return

        tab_labels: list[str] = []
        if has_source:
            tab_labels.append("查看源码")
        if has_tech:
            tab_labels.append("技术解读")
        if has_biz:
            tab_labels.append("业务解读")

        def _render_tabs() -> None:
            tabs = st.tabs(tab_labels)
            ti = 0
            if has_source:
                with tabs[ti]:
                    st.markdown("**方法源码片段**")
                    st.code(code or "", language=weaviate_svc.code_highlight_language())
                ti += 1
            if has_tech:
                with tabs[ti]:
                    st.markdown(
                        f"**技术解读**（{html.escape(inter.get('language') or 'zh')}）"
                    )
                    if inter.get("context_summary"):
                        st.caption("上下文摘要")
                        st.text(inter.get("context_summary") or "")
                    st.markdown(inter.get("interpretation_text") or "")
                ti += 1
            if has_biz:
                with tabs[ti]:
                    st.markdown(
                        f"**业务解读**（{html.escape(biz.get('language') or 'zh')}）"
                    )
                    bd = biz.get("business_domain") or ""
                    caps = biz.get("business_capabilities") or ""
                    if bd or caps:
                        st.caption(
                            f"业务域: {html.escape(bd)}  "
                            f"{'｜能力: ' + html.escape(caps) if caps else ''}"
                        )
                    st.markdown(biz.get("summary_text") or "")

        if wrap_in_expander:
            with st.expander("**解读专区**", expanded=True):
                _render_tabs()
        else:
            st.markdown("**解读专区**")
            _render_tabs()
