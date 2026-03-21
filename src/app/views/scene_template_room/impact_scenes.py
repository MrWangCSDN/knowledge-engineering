from __future__ import annotations

import streamlit as st

from src.app.views.scene_template_room.az_method_picker import render_az_method_picker
from src.app.views.scene_template_room.entity_detail_card import render_entity_detail_card
from src.app.views.scene_template_room.scene_base import SceneGraphGuardsMixin
from src.app.views.scene_template_room.impact_analysis_pure import (
    build_impact_node_rows,
    compute_impact_closure_set,
    impact_type_histogram_top,
    sorted_impact_node_rows,
    take_top_n,
)
from src.app.views.scene_template_room.scene_context import SceneTemplateContext


class ImpactAnalysisScene(SceneGraphGuardsMixin):
    key = "scene_impact_analysis"
    title = "方法变更影响分析"

    def render(self, ctx: SceneTemplateContext) -> None:
        with st.container(border=True):
            st.markdown(f"### {self.title}")
            st.caption(
                "选定**起点方法**，沿图中**全部有向边类型**做有界扩展（后继 / 前驱 / 双向），"
                "包括 **calls、implements、extends、belongs_to、depends_on** 等，用于估计变更波及的实体。"
            )

            st.markdown("**起点方法**（A–Z 分区 → 下拉，与「数据访问」等 Step 3 一致）")
            start_method_id = render_az_method_picker(
                ctx=ctx,
                key_prefix=f"{self.key}_start",
                exclude_methods_declared_on_interface=False,
            )

            direction = st.selectbox(
                "方向",
                options=["down(后继影响)", "up(前驱影响)", "both(双向=两次闭包合并)"],
                index=0,
                key=f"{self.key}_direction",
            )
            max_depth = st.slider(
                "最大深度（每条有向边计 1 层）",
                min_value=1,
                max_value=12,
                value=4,
                step=1,
                key=f"{self.key}_max_depth",
            )
            top_show = st.slider(
                "展示 TopN 节点",
                min_value=10,
                max_value=200,
                value=60,
                step=10,
                key=f"{self.key}_top_show",
            )
            run = st.button("运行", type="primary", key=f"{self.key}_run")

            if not run:
                st.info("选定起点后点击「运行」。")
                return

            backend = self.require_topology(ctx, purpose_cn="计算影响闭包")
            if backend is None:
                return

            if not start_method_id:
                st.warning("请用 A–Z 选择器选定起点方法。")
                return
            start = str(start_method_id).strip()

            mode = direction.split("(")[0]
            try:
                closure = compute_impact_closure_set(
                    backend, start, mode=mode, max_depth=int(max_depth)
                )
            except Exception:
                closure = {start}

            closure.discard(start)

            st.subheader("影响范围概览")
            st.write(f"闭包节点数：{len(closure)}")

            rows = build_impact_node_rows(closure, ctx.get_node)
            hist = impact_type_histogram_top(rows, top_k=8)

            st.markdown("**按实体类型分布（Top）**")
            parts = [f"`{t or 'unknown'}`：{c}" for t, c in hist]
            st.caption(" · ".join(parts) if parts else "—")

            st.subheader(f"展示节点（Top {int(top_show)}）")
            st.caption("每项为独立卡片：方法含源码/解读 Tab；类与接口以业务解读等为主（视向量库数据而定）。")
            ordered = sorted_impact_node_rows(rows)
            for r in take_top_n(ordered, int(top_show)):
                render_entity_detail_card(
                    ctx,
                    r.nid,
                    variant="impact",
                    entity_type=r.entity_type,
                    label=r.label,
                )
