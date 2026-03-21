"""按实体类型浏览：package / service / api_endpoint / A–Z 分区。"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional

import streamlit as st

from src.app.components.interpretation_panel import InterpretationPanel
from src.core.domain_enums import INTERP_PANEL_ENTITY_TYPES
from src.app.components.relation_table import RelationTableWithDetail
from src.app.utils.node_utils import format_node_display_label
from src.models.structure import EntityType


class OntologyBrowser:
    """按实体类型浏览图谱：包/服务/API 端点用下拉；类/方法等用 A–Z 分区。"""

    EXCLUDE_TYPES = {EntityType.MODULE, EntityType.FIELD, EntityType.PARAMETER}
    ZONE_ITEMS = [(chr(c), chr(c).lower()) for c in range(ord("A"), ord("Z") + 1)] + [
        ("其他", "other")
    ]

    def __init__(
        self,
        neo4j_backend: Any,
        key_prefix: str,
        weaviate_svc: Any,
        *,
        show_use_for_impact_button: bool = False,
        on_use_for_impact: Optional[Callable[[str], None]] = None,
    ):
        self._neo4j = neo4j_backend
        self._key_prefix = key_prefix
        self._weaviate_svc = weaviate_svc
        self._show_use_for_impact = show_use_for_impact_button
        self._on_use_for_impact = on_use_for_impact

    def render(self) -> Optional[str]:
        """
        渲染按类型浏览 UI，返回选中的节点 ID（若有）。
        """
        entity_type_options = [
            et.value for et in EntityType if et not in self.EXCLUDE_TYPES
        ]
        selected_type = st.selectbox(
            "选择实体类型",
            options=entity_type_options,
            key=f"{self._key_prefix}_entity_type",
        )
        if not selected_type:
            return None

        total = self._neo4j.count_nodes_by_entity_type(selected_type)
        nid = None

        if total == 0:
            st.info("该类型在 Neo4j 中暂无节点。")
            return None

        prev_key = f"{self._key_prefix}_entity_type_prev"
        if st.session_state.get(prev_key) != selected_type:
            st.session_state[prev_key] = selected_type
            # 仅在实体类型切换时重置分区/选择，避免每次 rerun 清空导致无法进入下拉列表
            st.session_state.pop(f"{self._key_prefix}_letter", None)
            st.session_state.pop(f"{self._key_prefix}_node_select", None)
            st.session_state.pop(f"{self._key_prefix}_node_select_pkg", None)
            st.session_state.pop(f"{self._key_prefix}_node_select_service", None)
            st.session_state.pop(f"{self._key_prefix}_node_select_api", None)

        stype = (selected_type or "").lower()

        if stype == "package":
            st.write(f"该类型共 **{total}** 个节点。选择包查看详情。")
            nodes_list = self._neo4j.list_nodes_by_entity_type("package", limit=500, skip=0)
            if not nodes_list:
                st.caption("暂无 package 节点。")
            else:
                idx = st.selectbox(
                    "选择节点查看详情与关系",
                    range(len(nodes_list)),
                    format_func=lambda i: nodes_list[i].get("name") or nodes_list[i].get("id") or "",
                    key=f"{self._key_prefix}_node_select_pkg",
                )
                nid = nodes_list[idx]["id"]

        elif stype == "service":
            st.write(f"该类型共 **{total}** 个节点。直接选择服务查看详情。")
            nodes_list = self._neo4j.list_nodes_by_entity_type("service", limit=500, skip=0)
            if not nodes_list:
                st.caption("暂无 service 节点。")
            else:
                idx = st.selectbox(
                    "选择节点查看详情与关系",
                    range(len(nodes_list)),
                    format_func=lambda i: nodes_list[i].get("name") or nodes_list[i].get("id") or "",
                    key=f"{self._key_prefix}_node_select_service",
                )
                nid = nodes_list[idx]["id"]

        elif stype == "api_endpoint":
            st.write(f"该类型共 **{total}** 个节点。先选**模块**，再在下方根据模块加载的列表中选择 API 端点。")
            st.session_state.pop(f"{self._key_prefix}_module_api", None)
            module_ids = self._neo4j.list_distinct_module_ids_for_entity_type("api_endpoint")
            if not module_ids:
                st.caption("暂无带模块信息的 api_endpoint 节点。")
            else:
                col_mod, col_api = st.columns(2)
                with col_mod:
                    selected_module = st.selectbox(
                        "选择模块",
                        options=module_ids,
                        key=f"{self._key_prefix}_module_api",
                    )
                nodes_list = self._neo4j.list_nodes_by_entity_type_and_module(
                    "api_endpoint", selected_module, limit=500, skip=0
                )
                with col_api:
                    if nodes_list:
                        idx = st.selectbox(
                            "选择 API 端点（根据左侧模块联动）",
                            range(len(nodes_list)),
                            format_func=lambda i: format_node_display_label(nodes_list[i]),
                            key=f"{self._key_prefix}_node_select_api",
                        )
                        nid = nodes_list[idx]["id"]
                    else:
                        st.selectbox(
                            "选择 API 端点（根据左侧模块联动）",
                            [],
                            key=f"{self._key_prefix}_node_select_api",
                        )
                st.caption(f"模块 **{selected_module}** 下共 **{len(nodes_list)}** 个 API 端点")

        else:
            st.write(f"该类型共 **{total}** 个节点。按名称首字母选分区，再在区内选择。")
            selected_letter = st.session_state.get(f"{self._key_prefix}_letter")
            buttons_per_row = 4
            for row in range(7):
                cols = st.columns(buttons_per_row)
                for col_idx in range(buttons_per_row):
                    idx = row * buttons_per_row + col_idx
                    if idx >= len(self.ZONE_ITEMS):
                        break
                    label, key = self.ZONE_ITEMS[idx]
                    with cols[col_idx]:
                        zone_count = self._neo4j.count_nodes_by_entity_type_and_prefix(
                            selected_type, key
                        )
                        if st.button(
                            f"{label} ({zone_count})",
                            key=f"{self._key_prefix}_zone_{key}",
                            type="primary" if selected_letter == key else "secondary",
                        ):
                            st.session_state[f"{self._key_prefix}_letter"] = key
                            st.rerun()

            letter = st.session_state.get(f"{self._key_prefix}_letter")
            if not letter:
                st.caption("请点击上方字母或「其他」选择分区。")
            else:
                zone_total = self._neo4j.count_nodes_by_entity_type_and_prefix(
                    selected_type, letter
                )
                zone_label = letter.upper() if letter != "other" else "其他"
                st.write(f"**{zone_label}** 区共 **{zone_total}** 条")
                if zone_total == 0:
                    st.caption("该区暂无节点。")
                    nodes_list = []
                else:
                    nodes_list = self._neo4j.list_nodes_by_entity_type_and_prefix(
                        selected_type, letter, limit=2000, skip=0
                    )
                search_key = f"{self._key_prefix}_zone_search_{letter}"
                keyword = (st.session_state.get(search_key) or "").strip()
                if keyword:
                    keyword_lower = keyword.lower()
                    filtered_list = [
                        n
                        for n in nodes_list
                        if keyword_lower in (format_node_display_label(n) or "").lower()
                    ]
                else:
                    filtered_list = nodes_list
                if filtered_list:
                    idx = st.selectbox(
                        "选择节点查看详情与关系",
                        range(len(filtered_list)),
                        format_func=lambda i: format_node_display_label(filtered_list[i]),
                        key=f"{self._key_prefix}_node_select",
                    )
                    nid = filtered_list[idx]["id"]
                elif nodes_list:
                    nid = None
                st.text_input(
                    "关键词筛选（缩小下拉列表）",
                    key=search_key,
                    placeholder="输入方法名、类名等…",
                )
                if keyword:
                    st.caption(
                        f"当前区共 {len(nodes_list)} 条，匹配「{keyword}」共 **{len(filtered_list)}** 条"
                    )
                elif len(nodes_list) > 100:
                    st.caption(f"当前区共 {len(nodes_list)} 条，可输入关键词缩小列表")
                if not filtered_list and nodes_list:
                    st.caption("无匹配项，请修改关键词或清空后重选。")

        if nid:
            node_detail = self._neo4j.get_node(nid)
            rels = self._neo4j.get_node_relations(nid)
            st.markdown("---")
            if self._show_use_for_impact and self._on_use_for_impact:
                if st.button(
                    "用于评估改动",
                    key=f"{self._key_prefix}_use_for_impact_%s"
                    % re.sub(r"[^a-zA-Z0-9]", "_", nid)[:60],
                    type="secondary",
                ):
                    self._on_use_for_impact(nid)
                    st.rerun()
            st.markdown("**节点属性**")
            st.json(node_detail or {"id": nid})
            # 兼容：部分节点详情缺失 entity_type 时，回退到当前选中的类型，避免解读面板被误隐藏
            etype = ((node_detail or {}).get("entity_type") or selected_type or "").lower()
            if etype in INTERP_PANEL_ENTITY_TYPES:
                InterpretationPanel.render(
                    nid, etype, node_detail, self._weaviate_svc
                )
            st.markdown("**出边（该节点 → 其他）**")
            if rels["outgoing"]:
                RelationTableWithDetail.render(
                    rels["outgoing"],
                    id_key="target_id",
                    name_key="target_name",
                    type_key="target_type",
                    key_prefix=f"{self._key_prefix}_out",
                    neo4j_backend=self._neo4j,
                    id_label="目标 ID",
                    name_label="目标名称",
                    type_label="目标类型",
                )
            else:
                st.caption("无")
            st.markdown("**入边（其他 → 该节点）**")
            if rels["incoming"]:
                RelationTableWithDetail.render(
                    rels["incoming"],
                    id_key="source_id",
                    name_key="source_name",
                    type_key="source_type",
                    key_prefix=f"{self._key_prefix}_in",
                    neo4j_backend=self._neo4j,
                    id_label="源 ID",
                    name_label="源名称",
                    type_label="源类型",
                )
            else:
                st.caption("无")

        return nid
