"""场景样板间（Graph + Vector 查询演示 Portal）。"""

from __future__ import annotations

from typing import Any

import streamlit as st

from src.app.services.app_services import AppServices
from src.app.views.scene_template_room.scene_context import build_scene_template_context
from src.app.views.scene_template_room.scene_registry import render_registered_scene_tabs


class SceneTemplateRoomView:
    """场景样板间 Portal（已接入查询逻辑的 UI 演示）。"""

    def __init__(
        self,
        *,
        services: AppServices,
        graph_backend: Any | None = None,
        neo4j_backend: Any | None = None,
    ) -> None:
        self._services = services
        self._graph_backend = graph_backend
        self._neo4j_backend = neo4j_backend

    def render(self) -> None:
        st.markdown("##### 场景样板间（Graph + Vector 查询演示）")
        st.caption("按你的场景需求组合：图谱遍历 / 向量检索 / 混合可解释 / 影响分析。")

        ctx = build_scene_template_context(
            services=self._services,
            graph_backend=self._graph_backend,
            neo4j_backend=self._neo4j_backend,
        )

        if not ctx.has_graph_backend():
            st.warning("当前未加载图谱后端（内存/Neo4j）。将影响需要图谱路径的场景。")

        render_registered_scene_tabs(ctx=ctx)
