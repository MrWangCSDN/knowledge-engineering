"""实体详情卡片：标题区 → 元数据 → 可选解读面板 / 源码片段 → 调试区（expander 或 checkbox+JSON）。"""
from __future__ import annotations

import hashlib
from typing import Any, Literal

import streamlit as st

from src.app.components.interpretation_panel import InterpretationPanel
from src.app.utils.node_utils import format_node_display_label
from src.core.domain_enums import INTERP_PANEL_ENTITY_TYPES
from src.app.views.scene_template_room.scene_context import SceneTemplateContext

VECTOR_SNIPPET_MISS_PREFIXES: tuple[str, ...] = (
    "// 未在向量库中找到该方法的源码片段",
    "// 未启用 Weaviate 源代码向量库",
    "// 从 Weaviate 获取源码失败",
)


def vector_snippet_is_miss(raw: str) -> bool:
    c = (raw or "").strip()
    if not c:
        return True
    return any(c.startswith(p) for p in VECTOR_SNIPPET_MISS_PREFIXES)


def build_impact_display_dict(
    ctx: SceneTemplateContext, nid: str, et: str, label: str, node: dict[str, Any]
) -> dict[str, Any]:
    if et == "method":
        d = ctx.method_listing_display(nid)
        n = ctx.get_node(nid) or node
        return {
            "id": nid,
            "entity_type": "method",
            "name": str(n.get("name") or d.get("title") or label).strip() or nid,
            "signature": str(d.get("signature") or n.get("signature") or "").strip(),
            "class_name": str(d.get("class_name") or n.get("class_name") or "").strip(),
            "module_id": n.get("module_id"),
        }
    return {
        "id": nid,
        "entity_type": et,
        "name": str(node.get("name") or label).strip() or nid,
        "signature": str(node.get("signature") or "").strip(),
        "class_name": str(node.get("class_name") or "").strip(),
        "module_id": node.get("module_id"),
    }


def build_method_display_dict(ctx: SceneTemplateContext, method_id: str) -> dict[str, Any]:
    n = ctx.get_node(method_id) or {}
    d = ctx.method_listing_display(method_id)
    return {
        "id": method_id,
        "entity_type": "method",
        "name": str(n.get("name") or d.get("title") or "").strip() or method_id,
        "signature": str(d.get("signature") or n.get("signature") or "").strip(),
        "class_name": str(d.get("class_name") or n.get("class_name") or "").strip(),
        "module_id": n.get("module_id"),
    }


def _entity_type_cn(et: str) -> str:
    return {
        "method": "方法",
        "class": "类",
        "interface": "接口",
        "api_endpoint": "API",
        "service": "服务",
        "file": "文件",
    }.get((et or "").lower(), et or "实体")


def render_entity_graph_meta_captions(node: dict[str, Any]) -> None:
    loc = str(node.get("location") or "").strip()
    mid = str(node.get("module_id") or "").strip()
    if loc:
        st.caption(f"位置：`{loc}`")
    if mid:
        st.caption(f"module_id：`{mid}`")


def render_method_code_snippet_with_fallback(
    ctx: SceneTemplateContext,
    nid: str,
    *,
    lang: str,
    node: dict[str, Any] | None = None,
) -> None:
    """向量库片段优先；miss 时用图谱签名/位置等组成占位代码块。"""
    n = node if node is not None else (ctx.get_node(nid) or {})
    code = (ctx.get_code_snippet(nid) or "").strip()
    if not vector_snippet_is_miss(code):
        st.code(code, language=lang)
        return
    sig = str(n.get("signature") or "").strip()
    loc = str(n.get("location") or "").strip()
    cn = str(n.get("class_name") or "").strip()
    if sig or loc or cn:
        lines: list[str] = []
        if loc:
            lines.append(f"// 图谱位置: {loc}")
        if cn:
            lines.append(f"// 类: {cn}")
        if sig:
            lines.append(sig if sig.endswith(";") else f"{sig};")
        st.code("\n".join(lines), language=lang)
        st.caption(
            "向量库无该方法片段：多为 **MyBatis Mapper 接口方法无 Java 方法体**，"
            "构建图时不会写入 `code_snippet`，故 CodeEntity 中无记录（**不是 ID 形态错误**）。"
            "上为图谱中的签名与位置；SQL 以 Mapper XML 为准，也可用「方法查表」查看。"
        )
    elif code:
        st.code(code, language=lang)
    else:
        st.caption("（向量库与图谱均未提供可展示片段）")


EntityCardVariant = Literal["impact", "method_snippet"]


def render_entity_detail_card(
    ctx: SceneTemplateContext,
    nid: str,
    *,
    variant: EntityCardVariant,
    entity_type: str = "",
    label: str = "",
    extra_heading_markdown: str | None = None,
    code_lang: str = "java",
    debug_in_expander: bool = True,
    debug_streamlit_key_suffix: str | None = None,
) -> None:
    """
    variant=impact：解读面板 + 图谱属性补充（与非 method/class/interface 类型一致）。
    variant=method_snippet：仅源码片段区（向量库 + 图谱兜底），无解读 Tab。

    ``debug_in_expander=False``：不在 ``st.expander`` 里再套 expander（例如父级已是 expander 时），
    改为 checkbox + JSON。此时建议传 ``debug_streamlit_key_suffix`` 保证页面内 key 唯一。
    """
    node = ctx.get_node(nid) or {}
    et = (entity_type or str(node.get("entity_type") or "")).lower() or ""

    if variant == "impact":
        disp = build_impact_display_dict(ctx, nid, et, label, node)
        et_cn = _entity_type_cn(et)
    else:
        disp = build_method_display_dict(ctx, nid)
        et_cn = _entity_type_cn("method")

    title = format_node_display_label(disp)

    with st.container(border=True):
        if extra_heading_markdown:
            st.markdown(extra_heading_markdown)
        st.markdown(f"##### {title}")
        st.caption(f"{et_cn} · `{nid}`")
        if variant == "impact":
            render_entity_graph_meta_captions(node)

        et_lower = et if variant == "impact" else "method"
        if variant == "impact":
            if et_lower in INTERP_PANEL_ENTITY_TYPES:
                st.caption(
                    "↓ **解读专区**：有数据时显示「查看源码 / 技术解读 / 业务解读」Tab；"
                    "若整块不出现，表示向量库中暂无该实体条目。"
                )
                InterpretationPanel.render(
                    nid,
                    et_lower,
                    node,
                    ctx.weaviate_data_svc,
                    wrap_in_expander=False,
                )
            else:
                st.markdown("**说明**")
                st.caption(
                    "该实体类型暂无「源码 / 技术解读 / 业务解读」统一面板；以下为图谱中已有属性。"
                )

            extra_lines: list[str] = []
            if et_lower not in ("method", "class", "interface"):
                for key in ("name", "signature", "class_name", "path", "description"):
                    v = node.get(key)
                    if v and str(v).strip():
                        extra_lines.append(f"- **{key}**：{v}")
            if extra_lines:
                st.markdown("**图谱属性**")
                st.markdown("\n".join(extra_lines))
        else:
            render_method_code_snippet_with_fallback(ctx, nid, lang=code_lang, node=node)

        payload = {k: v for k, v in node.items() if v is not None and k != "id"}
        if debug_in_expander:
            with st.expander("原始节点属性（调试）", expanded=False):
                st.json(payload)
        else:
            sk = debug_streamlit_key_suffix or hashlib.md5(
                f"{nid}|{variant}|{extra_heading_markdown or ''}".encode("utf-8")
            ).hexdigest()[:24]
            if st.checkbox("显示原始节点属性（调试）", value=False, key=f"raw_node_dbg_{sk}"):
                st.json(payload)
