"""场景样板间 Tab 注册表：Tab 标题 + 该 Tab 内要渲染的场景类（顺序即展示顺序）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import streamlit as st

if TYPE_CHECKING:
    from src.app.views.scene_template_room.scene_context import SceneTemplateContext


@dataclass(frozen=True)
class SceneTabSpec:
    """单个 Tab：标题 + 场景类列表（每个类无参构造，提供 .render(ctx)）。"""

    title: str
    scene_classes: tuple[type[Any], ...]


def render_registered_scene_tabs(*, ctx: "SceneTemplateContext") -> None:
    """按 SCENE_TAB_SPECS 渲染 Streamlit tabs 与场景（相邻场景间插入 divider）。"""
    specs = SCENE_TAB_SPECS
    tabs = st.tabs([s.title for s in specs])
    for i, spec in enumerate(specs):
        with tabs[i]:
            for j, SceneCls in enumerate(spec.scene_classes):
                if j:
                    st.divider()
                SceneCls().render(ctx)


# 延迟导入具体 Scene 类，避免 registry ↔ scene 模块循环依赖
def _load_scene_tab_specs() -> tuple[SceneTabSpec, ...]:
    from src.app.views.scene_template_room.graph_scenes import (
        CapabilityImplementationScene,
        KHopSemanticNeighborhoodScene,
        MethodCallRelationScene,
        MethodInterpretationScene,
        TermCodeLandingScene,
    )
    from src.app.views.scene_template_room.hybrid_scenes import EndToEndBusinessFlowScene
    from src.app.views.scene_template_room.impact_scenes import ImpactAnalysisScene
    from src.app.views.scene_template_room.table_access_scenes import (
        MethodToTableScene,
        TableToMethodScene,
    )
    from src.app.views.scene_template_room.vector_scenes import (
        BusinessQuestionToCodeScene,
        ReverseCodeToIntentScene,
    )

    return (
        SceneTabSpec(
            "图谱遍历场景",
            (
                MethodInterpretationScene,
                MethodCallRelationScene,
                CapabilityImplementationScene,
                TermCodeLandingScene,
                KHopSemanticNeighborhoodScene,
            ),
        ),
        SceneTabSpec(
            "向量检索场景",
            (BusinessQuestionToCodeScene, ReverseCodeToIntentScene),
        ),
        SceneTabSpec("混合可解释场景", (EndToEndBusinessFlowScene,)),
        SceneTabSpec("方法变更影响分析", (ImpactAnalysisScene,)),
        SceneTabSpec("数据访问（方法↔表）", (MethodToTableScene, TableToMethodScene)),
    )


SCENE_TAB_SPECS: tuple[SceneTabSpec, ...] = _load_scene_tab_specs()
