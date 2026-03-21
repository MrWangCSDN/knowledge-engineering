from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import streamlit as st

from src.knowledge.method_table_access_service import MethodTableAccessService
from src.app.views.scene_template_room.scene_context import SceneTemplateContext


class Scene(Protocol):
    key: str
    title: str

    def render(self, ctx: SceneTemplateContext) -> None: ...


@dataclass(frozen=True)
class _CardSpec:
    title: str
    subtitle: str


def render_card_container(*, spec: _CardSpec) -> None:
    with st.container(border=True):
        st.markdown(f"### {spec.title}")
        st.caption(spec.subtitle)


_TOPOLOGY_HINT = "请确认已构建图谱并连接 Neo4j 或内存图。"


def require_topology_backend(
    ctx: SceneTemplateContext,
    *,
    purpose_cn: str,
) -> Any | None:
    """需要「拓扑优先」后端（Neo4j 优先）时调用；缺失则 ``st.warning`` 并返回 ``None``。"""
    b = ctx.get_graph_backend_topology_primary()
    if b is None:
        st.warning(f"未加载图谱后端：无法{purpose_cn}（{_TOPOLOGY_HINT}）")
    return b


def require_memory_first_graph(
    ctx: SceneTemplateContext,
    *,
    purpose_cn: str | None = None,
    warning_message: str | None = None,
) -> Any | None:
    """需要「内存优先」后端时调用；缺失则提示并返回 ``None``。

    ``warning_message`` 非空时优先使用（保留场景特有措辞）；否则用 ``purpose_cn`` 拼模板句。
    """
    b = ctx.get_graph_backend_memory_first()
    if b is None:
        if warning_message:
            st.warning(warning_message)
        else:
            st.warning(
                f"未加载图谱后端：无法{purpose_cn or '使用图谱数据'}（{_TOPOLOGY_HINT}）"
            )
    return b


def require_method_table_svc(ctx: SceneTemplateContext) -> MethodTableAccessService | None:
    """方法↔表场景：缺少 schema 配置时提示并返回 ``None``。"""
    svc = ctx.method_table_access_svc
    if not svc:
        st.warning(
            "未配置 schema（repo.path + schema.ddl_path）。请在 config/project.yaml 中配置 repo 与 schema 段。"
        )
    return svc


class SceneGraphGuardsMixin:
    """样板间场景可选 Mixin：统一图谱 / schema 缺失时的 Streamlit 提示。"""

    def require_topology(self, ctx: SceneTemplateContext, *, purpose_cn: str) -> Any | None:
        return require_topology_backend(ctx, purpose_cn=purpose_cn)

    def require_memory_graph(
        self,
        ctx: SceneTemplateContext,
        *,
        purpose_cn: str | None = None,
        warning_message: str | None = None,
    ) -> Any | None:
        return require_memory_first_graph(
            ctx, purpose_cn=purpose_cn, warning_message=warning_message
        )

    def require_method_table(self, ctx: SceneTemplateContext) -> MethodTableAccessService | None:
        return require_method_table_svc(ctx)
