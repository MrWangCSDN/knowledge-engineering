"""场景样板间（Scene Template Room）组件集合。"""

from src.app.views.scene_template_room.scene_registry import (
    SCENE_TAB_SPECS,
    SceneTabSpec,
    render_registered_scene_tabs,
)

__all__ = ["SCENE_TAB_SPECS", "SceneTabSpec", "render_registered_scene_tabs"]
