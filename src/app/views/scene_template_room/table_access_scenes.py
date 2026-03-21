"""方法↔表访问场景：方法查表、表查方法。"""
from __future__ import annotations

from typing import Any

import streamlit as st

from src.app.utils.node_utils import format_node_display_label
from src.app.views.scene_template_room.az_method_picker import render_az_method_picker
from src.app.views.scene_template_room.entity_detail_card import render_entity_detail_card
from src.app.views.scene_template_room.scene_base import SceneGraphGuardsMixin
from src.app.views.scene_template_room.scene_context import SceneTemplateContext
from src.knowledge.method_table_access_service import (
    TableAccessDetail,
    TableAccessGrouped,
    format_method_table_debug_report,
)


def _method_display_dict(method_id: str, ctx: SceneTemplateContext) -> dict[str, Any]:
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


def _format_path_labels(path_ids: list[str], ctx: SceneTemplateContext) -> str:
    parts: list[str] = []
    for pid in path_ids:
        disp = _method_display_dict(pid, ctx)
        parts.append(format_node_display_label(disp))
    return " → ".join(parts)


def _op_label_cn(op: str) -> str:
    return {"select": "SELECT", "insert": "INSERT", "update": "UPDATE", "delete": "DELETE"}.get(
        op.lower(), op.upper()
    )


def _render_access_item(d: TableAccessDetail, ctx: SceneTemplateContext, svc: Any) -> None:
    st.markdown(f"**{d.mapper_statement}** · `{d.hop}` 跳")
    st.caption("路径（起点 → Mapper，中间可含类/模块等节点）：")
    st.markdown(_format_path_labels(d.path_method_ids, ctx))
    st.caption(f"Mapper method_id：`{d.source_method_id}`")
    if d.columns:
        st.caption(f"解析列：{', '.join(d.columns[:20])}{'…' if len(d.columns) > 20 else ''}")
    if d.sql_snippet:
        st.caption("Mapper SQL（片段）")
        st.code(d.sql_snippet, language="sql")


def _render_group(g: TableAccessGrouped, ctx: SceneTemplateContext, svc: Any) -> None:
    op_cn = _op_label_cn(g.op)
    n = len(g.items)
    if g.min_hop == g.max_hop:
        hop_str = f"最短 {g.min_hop} 跳"
    else:
        hop_str = f"最短 {g.min_hop} 跳 · 最远 {g.max_hop} 跳"
    title = f"`{g.table}` · {op_cn} · {hop_str}"
    if n > 1:
        title += f" · {n} 条 Mapper 语句"
    with st.expander(title, expanded=False):
        st.markdown("**表结构（DDL 摘要）**")
        st.code(svc.table_schema_text(g.table), language="text")
        for i, item in enumerate(g.items, start=1):
            st.markdown(f"---\n##### 语句 {i}/{n}")
            _render_access_item(item, ctx, svc)


class MethodToTableScene(SceneGraphGuardsMixin):
    key = "scene_method_to_table"
    title = "方法查表（谁读了什么、谁改了什么）"

    def render(self, ctx: SceneTemplateContext) -> None:
        with st.container(border=True):
            st.markdown(f"### {self.title}")
            st.caption(
                "从选中的方法出发，沿图中**所有类型出边**做无权 BFS（**每条有向边计 1 跳**），"
                "包括 calls、belongs_to、contains、depends_on 等，便于 Controller→Service→Mapper 等路径连通；"
                "**不沿 implements**（避免类→接口的横向扩散）；"
                "不向 term/domain/capability 类业务本体节点扩展，以免图爆炸。"
                "跳数 = 起点到「执行该 SQL 的 Mapper 方法」的**最短**路径长度（不含 Spring 代理/MyBatis 内部）。"
                "组内多条 Mapper 深度可能不同：标题会显示 **最短～最远**；展开后每条语句单独标跳数。"
            )

            st.markdown("**起点方法**")
            start_method_id = render_az_method_picker(
                ctx=ctx,
                key_prefix=f"{self.key}_start",
                exclude_methods_declared_on_interface=True,
            )

            max_hops = st.slider(
                "最大图遍历深度（沿出边步数）",
                min_value=2,
                max_value=12,
                value=8,
                step=1,
                key=f"{self.key}_max_hops",
            )

            run = st.button("运行", type="primary", key=f"{self.key}_run")

            if not run:
                st.info("选定起点方法后点击「运行」。")
                return

            svc = self.require_method_table(ctx)
            if not svc:
                return
            if not start_method_id:
                st.warning("请用 A–Z 选择器选定方法。")
                return
            backend = self.require_topology(ctx, purpose_cn="沿图出边做 BFS")
            if backend is None:
                return

            svc.load()
            svc.resolve_mapper_methods(backend)
            merge_b = ctx.get_graph_backend_topology_merge_secondary()
            result = svc.get_tables_for_method(
                start_method_id,
                backend,
                max_hops=int(max_hops),
                merge_backend=merge_b,
            )

            st.subheader("该方法（及调用链内）访问的表")
            st.caption(
                f"当前最大遍历深度上限：**{int(max_hops)}** 条出边（超过此深度的 Mapper 不会出现）。"
                "若结果偏多，可适当缩小深度或从更贴近持久层的起点方法查询。"
            )
            if not result.read_groups and not result.write_groups:
                st.caption(
                    "未发现表访问。可能原因：该方法的调用链内无 MyBatis Mapper 方法；"
                    "或 Mapper XML 未被正确解析；或 Mapper 方法与图谱中的 method 节点未对齐。"
                )
                with st.expander("诊断信息（便于排查：后端 / 起点后继 / Mapper 绑定）", expanded=False):
                    st.code(
                        format_method_table_debug_report(
                            backend=backend,
                            merge_backend=merge_b,
                            svc=svc,
                            start_method_id=start_method_id,
                            max_hops=int(max_hops),
                            knowledge_graph_backend=ctx.config_view.yaml_graph_backend,
                        ),
                        language="text",
                    )
                return

            col_read, col_write = st.columns(2)
            with col_read:
                st.markdown("**读（SELECT）**")
                if not result.read_groups:
                    st.caption("无")
                else:
                    for g in result.read_groups:
                        _render_group(g, ctx, svc)

            with col_write:
                st.markdown("**写（INSERT/UPDATE/DELETE）**")
                if not result.write_groups:
                    st.caption("无")
                else:
                    for g in result.write_groups:
                        _render_group(g, ctx, svc)


class TableToMethodScene(SceneGraphGuardsMixin):
    key = "scene_table_to_method"
    title = "表查方法（哪些方法访问了该表）"

    def render(self, ctx: SceneTemplateContext) -> None:
        with st.container(border=True):
            st.markdown(f"### {self.title}")
            st.caption(
                "输入表名（来自 DDL），列出 **读/写** 该表的 Mapper 方法及其上游可达方法。"
                "从 Mapper 出发沿图中**所有类型入边**反向 BFS（每边 1 跳），含 calls、belongs_to、contains、depends_on 等；"
                "**不沿 implements**；不踏入 term/domain/capability 节点；中间可经类、模块等非方法节点。"
                "展开后展示 **当前方法** 与 **执行 SQL 的 Mapper 方法** 的源码（来自向量库/结构层）。"
            )

            svc = self.require_method_table(ctx)
            if not svc:
                return

            svc.load()
            tables = svc.tables_sorted()
            if not tables:
                st.warning("DDL 未解析到表。请确认 schema.ddl_path 指向有效的 MySQL DDL 文件。")
                return

            table_options = ["(选择表)"] + tables
            idx = st.selectbox(
                "表名",
                options=range(len(table_options)),
                format_func=lambda i: table_options[i],
                key=f"{self.key}_table",
            )
            op_filter = st.radio(
                "操作类型",
                options=["全部", "读（SELECT）", "写（INSERT/UPDATE/DELETE）"],
                key=f"{self.key}_op",
                horizontal=True,
            )
            max_hops_tb = st.slider(
                "最大图回溯深度（沿入边步数）",
                min_value=2,
                max_value=12,
                value=8,
                step=1,
                key=f"{self.key}_max_hops",
            )
            run = st.button("运行", type="primary", key=f"{self.key}_run")

            if not run or idx == 0:
                st.info("选择表后点击「运行」。")
                return

            table_name = table_options[idx]
            backend = self.require_topology(ctx, purpose_cn="沿图入边回溯")
            if backend is None:
                return

            op_map = {"全部": None, "读（SELECT）": "read", "写（INSERT/UPDATE/DELETE）": "write"}
            methods = svc.get_methods_for_table(
                table_name,
                backend,
                op_filter=op_map.get(op_filter),
                max_hops=int(max_hops_tb),
                merge_backend=ctx.get_graph_backend_topology_merge_secondary(),
            )

            st.subheader(f"访问 `{table_name}` 的方法")
            st.code(svc.table_schema_text(table_name), language="text")
            if not methods:
                st.caption("未找到访问该表的方法。")
                return

            lang = ctx.weaviate_data_svc.code_highlight_language()

            for row_idx, m in enumerate(methods):
                disp = _method_display_dict(m.method_id, ctx)
                label = format_node_display_label(disp)
                op_label = "读" if m.op == "select" else "写"
                with st.expander(f"{label} · {op_label} · {m.hop} 跳", expanded=False):
                    st.caption(f"当前方法 method_id：`{m.method_id}`")
                    st.caption(f"直接执行 SQL 的 Mapper method_id：`{m.source_method_id}`")
                    render_entity_detail_card(
                        ctx,
                        m.method_id,
                        variant="method_snippet",
                        extra_heading_markdown="**当前方法 · 源码**",
                        code_lang=lang,
                        debug_in_expander=False,
                        debug_streamlit_key_suffix=f"{self.key}_r{row_idx}_cur",
                    )
                    if m.source_method_id != m.method_id:
                        render_entity_detail_card(
                            ctx,
                            m.source_method_id,
                            variant="method_snippet",
                            extra_heading_markdown="**Mapper 方法 · 源码**",
                            code_lang=lang,
                            debug_in_expander=False,
                            debug_streamlit_key_suffix=f"{self.key}_r{row_idx}_map",
                        )
