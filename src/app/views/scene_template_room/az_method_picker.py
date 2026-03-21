from __future__ import annotations

from typing import Any

import streamlit as st

from src.app.utils.node_utils import format_node_display_label
from src.app.views.scene_template_room.scene_base import require_topology_backend
from src.knowledge.graph_neo4j import Neo4jGraphBackend
from src.knowledge.method_entity_id_normalize import method_entity_id_variants


def _method_declared_on_interface(backend: Any, method_id: str | None) -> bool:
    """method 经 BELONGS_TO 指向的类节点若为 interface，则视为接口上的方法。"""
    if not backend or not method_id:
        return False
    succ = getattr(backend, "successors", None)
    getn = getattr(backend, "get_node", None)
    if not callable(succ):
        return False
    for vid in method_entity_id_variants(str(method_id)) or [str(method_id)]:
        try:
            targets = succ(vid, rel_type="belongs_to")
        except Exception:
            targets = []
        for tid in targets or []:
            if not callable(getn):
                continue
            try:
                cn = getn(tid)
            except Exception:
                cn = None
            if cn and str(cn.get("entity_type") or "").lower() == "interface":
                return True
    return False


def _letters() -> list[tuple[str, str]]:
    # （button 展示使用大写）+（session state 存小写/other）
    out: list[tuple[str, str]] = []
    for c in range(ord("A"), ord("Z") + 1):
        ch = chr(c)
        out.append((ch, ch.lower()))
    out.append(("其他", "other"))
    return out


def render_az_method_picker(
    *,
    ctx: Any,
    key_prefix: str,
    limit: int = 2000,
    exclude_methods_declared_on_interface: bool = False,
) -> str | None:
    """
    返回选中的 method 节点 id（method://...）。
    使用方式：放在场景的「参数输入」区域。
    exclude_methods_declared_on_interface：为 True 时去掉 BELONGS_TO 指向 interface 的方法（如仅方法查表场景）。
    """
    backend = require_topology_backend(ctx, purpose_cn="做 A–Z 分块选择")
    if backend is None:
        return None

    if exclude_methods_declared_on_interface:
        st.caption(
            "起点列表已**排除声明在接口（interface）上的方法**（仅保留实现类等上的方法），便于沿 calls 边定位 Mapper。"
        )

    # Neo4jGraphBackend：支持 count/list + prefix 分区
    has_prefix_api = hasattr(backend, "count_nodes_by_entity_type_and_prefix") and hasattr(
        backend, "list_nodes_by_entity_type_and_prefix"
    )

    if not has_prefix_api:
        # 备选：search_by_name + selectbox（无前缀分区 API 时）
        st.caption("当前后端不支持 A–Z 分区，已切换为按关键词从图谱中检索方法。")
        kw_key = f"{key_prefix}_keyword"
        kw = st.text_input("方法名/签名关键词", key=kw_key, placeholder="输入关键词后从图谱搜索方法节点…")
        if not kw or not hasattr(backend, "search_by_name"):
            return None
        try:
            hits = backend.search_by_name(kw, entity_types=["method"], limit=50)  # type: ignore[attr-defined]
        except Exception:
            hits = []
        if exclude_methods_declared_on_interface:
            hits = [h for h in hits if not _method_declared_on_interface(backend, str(h.get("id") or ""))]
        if not hits:
            st.caption("无匹配方法。")
            return None
        idx = st.selectbox(
            "选择方法",
            options=range(len(hits)),
            format_func=lambda i: format_node_display_label(hits[i]),
            key=f"{key_prefix}_node_select",
        )
        return str(hits[idx].get("id") or "")

    letter_key = f"{key_prefix}_letter"
    kw_in_zone_key = f"{key_prefix}_kw_in_zone"
    module_filter_key = f"{key_prefix}_module_filter"

    # 分区按钮
    buttons_per_row = 5
    letters = _letters()
    # grid：把展示区分成若干行
    row_cnt = (len(letters) + buttons_per_row - 1) // buttons_per_row
    for row in range(row_cnt):
        cols = st.columns(buttons_per_row)
        for col_idx in range(buttons_per_row):
            idx = row * buttons_per_row + col_idx
            if idx >= len(letters):
                break
            label, st_val = letters[idx]
            with cols[col_idx]:
                try:
                    if isinstance(backend, Neo4jGraphBackend):
                        cnt = backend.count_nodes_by_entity_type_and_prefix(
                            "method",
                            st_val,
                            exclude_methods_on_interface=exclude_methods_declared_on_interface,
                        )
                    else:
                        cnt = backend.count_nodes_by_entity_type_and_prefix("method", st_val)  # type: ignore[attr-defined]
                except Exception:
                    cnt = 0
                clicked = st.button(
                    f"{label} ({cnt})",
                    key=f"{key_prefix}_btn_{st_val}",
                    type="primary" if st.session_state.get(letter_key) == st_val else "secondary",
                    disabled=cnt == 0,
                    use_container_width=True,
                )
                if clicked:
                    st.session_state[letter_key] = st_val
                    # 选择字母后立刻刷新，以展示对应分区下拉列表
                    st.rerun()

    selected_letter = st.session_state.get(letter_key)
    if not selected_letter:
        st.caption("点击上方字母或「其他」选择分区。")
        return None

    # 取该字母分区的候选 method
    try:
        if isinstance(backend, Neo4jGraphBackend):
            zone_nodes = backend.list_nodes_by_entity_type_and_prefix(
                "method",
                selected_letter,
                limit=limit,
                skip=0,
                exclude_methods_on_interface=exclude_methods_declared_on_interface,
            )
        else:
            zone_nodes = backend.list_nodes_by_entity_type_and_prefix(  # type: ignore[attr-defined]
                "method", selected_letter, limit=limit, skip=0
            )
    except Exception:
        zone_nodes = []

    if exclude_methods_declared_on_interface and zone_nodes and not isinstance(backend, Neo4jGraphBackend):
        zone_nodes = [
            n
            for n in zone_nodes
            if not _method_declared_on_interface(backend, str(n.get("id") or ""))
        ]

    if not zone_nodes:
        st.caption("当前分区暂无方法。")
        return None

    kw_in_zone = (st.session_state.get(kw_in_zone_key) or "").strip()
    module_filter = (st.session_state.get(module_filter_key) or "").strip()

    # keyword/module 在此处用于过滤下拉列表
    kw_in_zone = st.text_input(
        "关键词筛选（缩小下拉列表）",
        key=kw_in_zone_key,
        placeholder="输入方法名/签名片段…（可选）",
    ).strip()
    module_filter = st.text_input(
        "module_id 过滤（可选）",
        key=module_filter_key,
        placeholder="如 mall-portal（可选）",
    ).strip()

    filtered = zone_nodes
    if kw_in_zone:
        kw_lower = kw_in_zone.lower()
        filtered = [n for n in filtered if kw_lower in (format_node_display_label(n) or "").lower()]
    if module_filter:
        mf_lower = module_filter.lower()
        filtered = [
            n for n in filtered if mf_lower in (str(n.get("module_id") or "").lower())
        ]

    if not filtered:
        st.caption("过滤后无匹配方法。")
        return None

    st.caption(f"当前分区：{selected_letter}，候选数量：{len(filtered)}")
    idx = st.selectbox(
        "选择方法（下拉）",
        options=range(len(filtered)),
        format_func=lambda i: format_node_display_label(filtered[i]),
        key=f"{key_prefix}_node_select",
    )
    return str(filtered[idx].get("id") or "")

