from __future__ import annotations

from collections import deque
from typing import Any

import streamlit as st

from src.app.components.interpretation_panel import InterpretationPanel
from src.app.views.scene_template_room.scene_base import SceneGraphGuardsMixin
from src.app.views.scene_template_room.scene_context import SceneTemplateContext
from src.knowledge.method_entity_id_normalize import normalize_method_entity_id


def _bfs_neighbors(
    backend: Any,
    start: str,
    *,
    rel_types: list[str] | None = None,
    direction: str = "both",
    max_depth: int = 3,
    max_nodes: int = 80,
) -> dict[str, int]:
    """
    简化版图遍历：以 successor/predecessor 为邻接；支持 rel_types 过滤（按 rel_type 逐个展开，去重）。
    返回 {nid: dist}，包含 start（dist=0）。
    """
    rel_types = rel_types or []
    dist: dict[str, int] = {start: 0}
    q = deque([(start, 0)])

    while q and len(dist) < max_nodes:
        nid, d = q.popleft()
        if d >= max_depth:
            continue

        next_ids: list[str] = []
        for rt in (rel_types or [None]):
            # rt=None 表示不过滤
            try:
                if direction in ("down", "both"):
                    next_ids.extend(backend.successors(nid, rel_type=rt) if rt else backend.successors(nid))  # type: ignore[misc]
                if direction in ("up", "both"):
                    next_ids.extend(backend.predecessors(nid, rel_type=rt) if rt else backend.predecessors(nid))  # type: ignore[misc]
            except Exception:
                continue

        for nx in next_ids:
            if not nx or nx in dist:
                continue
            dist[nx] = d + 1
            q.append((nx, d + 1))
            if len(dist) >= max_nodes:
                break

    return dist


def _extract_module_ids_from_service_ids(service_ids: list[str]) -> list[str]:
    out: list[str] = []
    for sid in service_ids:
        s = (sid or "").strip()
        if not s:
            continue
        if s.startswith("service://"):
            out.append(s.replace("service://", "", 1))
        else:
            out.append(s)
    # uniq preserve
    return list(dict.fromkeys([x for x in out if x]))


def _method_id_from_api_endpoint(api_id: str, api_node: dict | None) -> str | None:
    """api_endpoint 节点在结构层带 method_entity_id；id 亦可能为 {method_id}#api。"""
    if api_node:
        mid = str(api_node.get("method_entity_id") or "").strip()
        if mid:
            return normalize_method_entity_id(mid)
    aid = (api_id or "").strip()
    if aid.endswith("#api"):
        base = aid[: -len("#api")].strip()
        if base:
            return normalize_method_entity_id(base)
    return None


def _api_expander_label(ctx: SceneTemplateContext, api_id: str, api_node: dict | None, idx: int) -> str:
    """列表行标题：方法签名（类名），避免仅 path 造成歧义。"""
    path_hint = ""
    if api_node:
        path_hint = str(api_node.get("name") or "").strip()
    if not path_hint:
        path_hint = ctx.get_node_name(api_id)
    mid = _method_id_from_api_endpoint(api_id, api_node)
    if mid:
        d = ctx.method_listing_display(mid)
        sig = (d.get("signature") or "").strip()
        cls = (d.get("class_name") or "").strip()
        if sig:
            return f"{idx}. {sig}（{cls or '未知类'}）"
        t = (d.get("title") or "").strip()
        if t:
            return f"{idx}. {t}（{cls or '未知类'}）"
    mn = str((api_node or {}).get("method_name") or "").strip()
    cn = str((api_node or {}).get("class_name") or "").strip()
    if mn or cn:
        return f"{idx}. {mn or path_hint}（{cn or '未知类'}）"
    return f"{idx}. {path_hint or api_id}"


class EndToEndBusinessFlowScene(SceneGraphGuardsMixin):
    key = "scene_end_to_end_flow"
    title = "端到端业务流程查询（域 -> 多模块协作 -> 流程链）"

    def render(self, ctx: SceneTemplateContext) -> None:
        with st.container(border=True):
            st.markdown(f"### {self.title}")
            st.caption("给定业务域（或能力），沿 service_calls（服务调用）做链式展开，再回拉 service_exposes（对外 API）。")

            with st.form("form_end_to_end_flow", clear_on_submit=False):
                domain_input = st.text_input("business_domain（domain://... 或域名称）", value="domain://后台管理域")
                max_depth = st.slider("服务调用链深度", min_value=1, max_value=6, value=3, step=1)
                top_show = st.slider("展示 TopN（服务节点）", min_value=5, max_value=60, value=20, step=5)
                run = st.form_submit_button("运行", type="primary")

            if not run:
                return

            backend = self.require_memory_graph(
                ctx,
                warning_message="未加载图谱后端：无法计算端到端服务链。",
            )
            if backend is None:
                return

            domain_raw = (domain_input or "").strip()
            if not domain_raw:
                st.warning("请输入业务域。")
                return

            domain_nid = domain_raw if domain_raw.startswith("domain://") else f"domain://{domain_raw}"

            # 从域回拉 service
            try:
                service_ids = backend.predecessors(domain_nid, rel_type="BELONGS_TO_DOMAIN")
            except Exception:
                service_ids = []

            if not service_ids:
                st.warning("未查询到该业务域对应的 BELONGS_TO_DOMAIN 服务节点。请检查输入是否正确或图谱同步是否完成。")
                return

            # 多源 BFS：从每个起始 service 沿 service_calls 展开
            dist_all: dict[str, int] = {}
            for sid in service_ids:
                dist = _bfs_neighbors(
                    backend,
                    sid,
                    rel_types=["service_calls"],
                    direction="down",
                    max_depth=int(max_depth),
                    max_nodes=150,
                )
                for k, v in dist.items():
                    if k not in dist_all or v < dist_all[k]:
                        dist_all[k] = v

            # 过滤掉起点集合中重复，最终按 dist 展示
            service_nodes = list(dist_all.keys())
            service_nodes.sort(key=lambda x: dist_all.get(x, 999))
            service_nodes = [x for x in service_nodes if x]  # type: ignore
            service_nodes = service_nodes[: int(top_show)]

            st.subheader("服务协作链（service_calls）")
            for sid in service_nodes:
                st.write(f"- dist={dist_all.get(sid)} | {ctx.get_node_name(sid)} (`{sid}`)")

            # 回拉 service_exposes -> api_endpoint
            st.subheader("每个服务暴露的 API（service_exposes）")
            st.caption(
                "每条以「方法签名（类名）」展示；展开后查看 **解读专区**（技术解读 / 业务解读 / 源码片段，视向量库数据而定）。"
                "若未出现解读区块，说明流水线尚未写入或未配置 Weaviate。"
            )
            max_services_for_apis = min(15, len(service_nodes))
            max_apis_per_service = 40
            for sid in service_nodes[:max_services_for_apis]:
                apis: list[str] = []
                try:
                    apis = backend.successors(sid, rel_type="service_exposes")
                except Exception:
                    apis = []
                if not apis:
                    st.caption(f"{ctx.get_node_name(sid)}：无 service_exposes 结果")
                    continue
                with st.container(border=True):
                    st.markdown(f"**{ctx.get_node_name(sid)}** · `{sid}`")
                    shown = apis[:max_apis_per_service]
                    if len(apis) > len(shown):
                        st.caption(
                            f"仅展示前 {len(shown)} / {len(apis)} 条 API（当前为内置展示上限，避免单页过长）。"
                        )
                    for i, aid in enumerate(shown, start=1):
                        api_node = ctx.get_node(aid)
                        label = _api_expander_label(ctx, aid, api_node, i)
                        mid = _method_id_from_api_endpoint(aid, api_node)
                        path_disp = str((api_node or {}).get("name") or "").strip() or ctx.get_node_name(aid)
                        with st.expander(label, expanded=False):
                            st.caption(f"HTTP path：`{path_disp}`")
                            st.caption(f"api_endpoint：`{aid}`")
                            if mid:
                                st.caption(f"method：`{mid}`")
                                method_node = ctx.get_node(mid)
                                InterpretationPanel.render(
                                    mid,
                                    "method",
                                    method_node,
                                    ctx.weaviate_data_svc,
                                    wrap_in_expander=False,
                                )
                            else:
                                st.info(
                                    "该 API 在图谱中未解析到 method_entity_id，无法关联方法与解读；"
                                    "请确认结构事实中 api_endpoint 是否带 method_entity_id（Java 解析器会写入）。"
                                )
