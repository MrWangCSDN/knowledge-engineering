from __future__ import annotations

from typing import Any, Iterable

import streamlit as st

from src.app.views.scene_template_room.scene_base import SceneGraphGuardsMixin
from src.app.views.scene_template_room.scene_context import SceneTemplateContext
from src.app.views.scene_template_room.az_method_picker import render_az_method_picker
from src.app.views.scene_template_room.method_call_relation_graph_view import render_method_call_relation_graph


def _entity_type_lower(node: dict[str, Any] | None) -> str:
    if not node:
        return ""
    return str(node.get("entity_type") or "").lower()


def _uniq_preserve_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def _resolve_method_class_ids(ctx: SceneTemplateContext, method_id: str) -> list[str]:
    b = ctx.get_graph_backend_memory_first()
    if b is None:
        return []
    try:
        return [x for x in b.successors(method_id, rel_type="belongs_to") if x]
    except Exception:
        return []


class MethodInterpretationScene:
    key = "scene_method_interpret"
    title = "解读给定的 method"

    def render(self, ctx: SceneTemplateContext) -> None:
        with st.container(border=True):
            st.markdown(f"### {self.title}")
            st.caption("通过 A–Z 分区选择方法（无需知道 `method://` 完整 id）。展示：源码片段、技术解读，以及图谱邻居。")

            method_id = render_az_method_picker(ctx=ctx, key_prefix="scene_method_interpret")
            run_clicked = st.button("运行", type="primary", use_container_width=True, key="scene_method_interpret_run")
            if not run_clicked:
                st.info("选择一个方法后点击「运行」。")
                return

            resolved_method = method_id or ""
            if not resolved_method:
                st.warning("请选择一个方法。")
                return

            backend = ctx.get_graph_backend_memory_first()
            st.markdown("---")
            st.subheader("源码片段")
            lang = ctx.services.weaviate_data_svc.code_highlight_language()
            snippet = ctx.get_code_snippet(resolved_method)
            st.code(snippet, language=lang)

            st.subheader("技术解读（来自 Weaviate）")
            tech = ctx.weaviate_data_svc.fetch_method_interpretation(resolved_method)
            if tech:
                st.write(tech.get("interpretation_text") or "")
                if tech.get("context_summary"):
                    st.caption("上下文摘要：")
                    st.write(tech.get("context_summary"))
                if tech.get("related_entity_ids_json"):
                    st.caption("关联实体（原始字段）：")
                    st.json(tech.get("related_entity_ids_json"))
            else:
                st.caption("暂无技术解读（请确认已运行 method_interpretation + vectordb-interpret）。")

            st.subheader("图谱邻居（快速摘要）")
            if backend is None:
                st.caption("当前未加载图谱后端：仅展示向量库源码与技术解读。")
                return

            classes = _resolve_method_class_ids(ctx, resolved_method)
            called = []
            callers = []
            domains = []
            try:
                called = backend.successors(resolved_method, rel_type="calls")  # type: ignore[call-arg]
                callers = backend.predecessors(resolved_method, rel_type="calls")  # type: ignore[call-arg]
                domains = backend.successors(resolved_method, rel_type="IN_DOMAIN")  # type: ignore[call-arg]
            except Exception:
                pass

            if classes:
                st.write("所属类/接口：")
                st.write([ctx.get_node_name(cid) for cid in classes[:5]])
            else:
                st.caption("所属类/接口：未查询到（或当前图谱缺失相关边）。")

            st.write(f"直接调用（calls → out）TopN：{len(called)}")
            if called:
                st.write([ctx.get_node_name(cid) for cid in called[:10]])
            st.write(f"直接被调用（calls → in）TopN：{len(callers)}")
            if callers:
                st.write([ctx.get_node_name(cid) for cid in callers[:10]])

            if domains:
                st.write("相关业务域（IN_DOMAIN）TopN：")
                st.write([ctx.get_node_name(did) for did in domains[:10]])
            else:
                st.caption("相关业务域：未查询到 IN_DOMAIN 关系。")


class MethodCallRelationScene(SceneGraphGuardsMixin):
    key = "scene_method_call_relation"
    title = "方法调用关系（调用方/被调用方链）"

    def render(self, ctx: SceneTemplateContext) -> None:
        with st.container(border=True):
            st.markdown(f"### {self.title}")
            st.caption("沿调用边（calls）做 up/down 多跳展开，并限制深度与展示数量。")

            selected_method = render_az_method_picker(ctx=ctx, key_prefix="scene_method_call_relation")
            direction = st.selectbox(
                "方向",
                options=["down(被调用)", "up(调用方)", "both(双向)"],
                index=0,
                key="scene_method_call_relation_dir",
            )
            max_depth = st.slider("最大深度", min_value=1, max_value=6, value=3, step=1, key="scene_method_call_relation_depth")
            max_nodes = st.slider("最多展示节点数", min_value=10, max_value=200, value=60, step=10, key="scene_method_call_relation_nodes")

            run_clicked = st.button(
                "运行",
                type="primary",
                use_container_width=True,
                key="scene_method_call_relation_run",
            )
            if not run_clicked:
                st.info("选择一个方法后点击「运行」。")
                return

            backend = self.require_memory_graph(ctx, purpose_cn="做 calls 有向遍历")
            if backend is None:
                return

            method_id = selected_method or ""
            if not method_id:
                st.warning("请选择一个方法。")
                return

            dir_key = direction.split("(")[0]
            if dir_key.startswith("down"):
                mode = "down"
            elif dir_key.startswith("up"):
                mode = "up"
            else:
                mode = "both"

            # BFS：限制 depth 与总节点数
            from collections import deque

            queue = deque([(method_id, 0)])
            seen: dict[str, int] = {method_id: 0}
            out_nodes: list[str] = []

            while queue and len(out_nodes) < int(max_nodes):
                nid, d = queue.popleft()
                if d >= int(max_depth):
                    continue

                next_ids: list[str] = []
                try:
                    if mode in ("down", "both"):
                        next_ids.extend(backend.successors(nid, rel_type="calls"))
                    if mode in ("up", "both"):
                        next_ids.extend(backend.predecessors(nid, rel_type="calls"))
                except Exception:
                    next_ids = []

                for nx in next_ids:
                    if not nx or nx in seen:
                        continue
                    nd = d + 1
                    seen[nx] = nd
                    queue.append((nx, nd))

            # 去掉起点
            out_nodes = [x for x in seen.keys() if x != method_id]
            # 按最短距离排序
            out_nodes.sort(key=lambda x: seen.get(x, 999))
            out_nodes = out_nodes[: int(max_nodes)]

            st.subheader("展开结果概览")
            st.write(f"中心方法：`{method_id}`")
            st.write(f"可达节点数（不含起点）：{len(out_nodes)}")

            # 组装图的边：仅在“可达节点集合”内部画 calls 边，避免图爆炸
            node_set = set(out_nodes) | {method_id}
            edge_source_to_targets: dict[str, list[str]] = {}
            for s in node_set:
                try:
                    outs = backend.successors(s, rel_type="calls")
                except Exception:
                    outs = []
                edge_source_to_targets[s] = [t for t in (outs or []) if t in node_set and t != s]

            render_method_call_relation_graph(
                ctx=ctx,
                start_method_id=method_id,
                seen_dist=seen,
                node_ids=out_nodes,
                edge_source_to_targets=edge_source_to_targets,
            )


class CapabilityImplementationScene(SceneGraphGuardsMixin):
    key = "scene_capability_overview"
    title = "某业务能力的实现全景图（能力 -> 模块 -> 方法）"

    def render(self, ctx: SceneTemplateContext) -> None:
        with st.container(border=True):
            st.markdown(f"### {self.title}")
            st.caption("能力（capability）→ 域/服务 → 方法的关联路径；向量库用于对候选方法排序。")

            with st.form("form_capability_overview", clear_on_submit=False):
                capability_input = st.text_input(
                    "能力（可填能力名称，或图谱中 capability 节点的 id）", value="商品管理"
                )
                limit_modules = st.slider("每个域最多关联模块数（展示用）", min_value=1, max_value=50, value=10, step=1)
                limit_methods = st.slider("最终候选方法数量上限（排序后展示）", min_value=10, max_value=200, value=60, step=10)
                run = st.form_submit_button("运行", type="primary")

            if not run:
                st.info("填写输入并点击「运行」。")
                return

            backend = self.require_memory_graph(
                ctx,
                warning_message="当前未加载图谱后端：仅可做向量相似排序，无法得到能力->模块->方法的结构化链路。",
            )
            if backend is None:
                return

            cap_id = str(capability_input).strip()
            cap_nid = ctx.capability_nid(cap_id)

            domains: list[str] = []
            try:
                domains = backend.predecessors(cap_nid, rel_type="CONTAINS_CAPABILITY")  # type: ignore[call-arg]
            except Exception:
                domains = []

            if not domains:
                st.warning("未查询到 capability -> domain 的 CONTAINS_CAPABILITY 关系。请确认图谱已同步到 Neo4j 或内存图包含该能力节点。")
                return

            service_ids: list[str] = []
            for did in domains[: int(limit_modules)]:
                try:
                    service_ids.extend(backend.predecessors(did, rel_type="BELONGS_TO_DOMAIN"))  # type: ignore[call-arg]
                except Exception:
                    continue
            service_ids = _uniq_preserve_order([s for s in service_ids if s])

            module_ids: list[str] = []
            for sid in service_ids:
                if str(sid).startswith("service://"):
                    module_ids.append(str(sid).replace("service://", "", 1))
            module_ids = _uniq_preserve_order([m for m in module_ids if m])[: int(limit_modules)]

            # 收集方法候选
            candidate_methods: list[str] = []
            for mid in module_ids:
                try:
                    if hasattr(backend, "list_nodes_by_entity_type_and_module"):
                        rows = backend.list_nodes_by_entity_type_and_module("method", mid, limit=200, skip=0)  # type: ignore[attr-defined]
                        candidate_methods.extend([r.get("id") for r in rows if r.get("id")])
                    elif hasattr(backend, "iter_nodes"):
                        for nid, attrs in backend.iter_nodes():  # type: ignore[attr-defined]
                            if (attrs or {}).get("entity_type") == "method" and (attrs or {}).get("module_id") == mid:
                                candidate_methods.append(nid)
                    # 内层不做全量限制，最后再截断
                except Exception:
                    continue

            candidate_methods = _uniq_preserve_order([x for x in candidate_methods if x])[:2000]

            st.subheader("候选方法排序（向量相似度）")
            if ctx.code_vector_store is None:
                st.caption("未启用 code 向量库：无法做向量重排，将按候选列表顺序展示。")
                ranked = candidate_methods[: int(limit_methods)]
            else:
                try:
                    hits = ctx.code_vector_store.search_by_text(cap_id, top_k=max(50, int(limit_methods)))
                    # hits: [(eid, score)]
                    hit_ids = [eid for eid, _score in hits]
                    ranked = [eid for eid in hit_ids if eid in set(candidate_methods)][: int(limit_methods)]
                    if not ranked:
                        ranked = candidate_methods[: int(limit_methods)]
                except Exception:
                    ranked = candidate_methods[: int(limit_methods)]

            st.write("关联域：")
            st.write([ctx.get_node_name(did) for did in domains[:10]])
            st.write("关联模块：")
            st.write(module_ids[: int(limit_modules)])

            st.subheader("Top 方法列表（含源码片段预览）")
            for midx, eid in enumerate(ranked[:3], start=1):
                node = ctx.get_node(eid) or {}
                st.write(f"{midx}. `{eid}`")
                if node.get("name"):
                    st.caption(f"方法名：{node.get('name')}")
                snippet = ctx.get_code_snippet(eid)
                st.code(snippet, language=ctx.weaviate_data_svc.code_highlight_language())


class TermCodeLandingScene(SceneGraphGuardsMixin):
    key = "scene_term_code_landing"
    title = "某术语（term）对应的代码落点（term -> RELATED_TO -> code）"

    def render(self, ctx: SceneTemplateContext) -> None:
        with st.container(border=True):
            st.markdown(f"### {self.title}")
            st.caption("从术语（term）节点沿 RELATED_TO 取相关代码实体，并按向量相似度排序展示。")

            with st.form("form_term_code_landing", clear_on_submit=False):
                term_input = st.text_input(
                    "术语（可填业务词如 商品/订单，或图谱中 term 节点的 id）", value="商品"
                )
                limit = st.slider("展示数量 TopN", min_value=5, max_value=50, value=15, step=5)
                run = st.form_submit_button("运行", type="primary")

            if not run:
                st.info("填写输入并点击「运行」。")
                return

            backend = self.require_memory_graph(ctx, purpose_cn="计算 term -> RELATED_TO 的落点")
            if backend is None:
                return

            term_id = str(term_input).strip()
            term_nid = ctx.term_nid(term_id)
            candidates: list[str] = []
            try:
                candidates = backend.successors(term_nid, rel_type="RELATED_TO")  # type: ignore[call-arg]
            except Exception:
                candidates = []

            # 过滤 code 实体类型
            filtered: list[str] = []
            for eid in candidates:
                n = ctx.get_node(eid)
                et = _entity_type_lower(n)
                if et in ("method", "class", "interface"):
                    filtered.append(eid)
            filtered = _uniq_preserve_order(filtered)

            st.subheader("图谱落点概览")
            st.write(f"术语节点：`{term_nid}`")
            st.write(f"候选代码实体数：{len(filtered)}")

            ranked = filtered[: int(limit)]
            if ctx.code_vector_store is not None and filtered:
                try:
                    hits = ctx.code_vector_store.search_by_text(term_id, top_k=max(20, int(limit)))
                    hit_ids = [eid for eid, _score in hits]
                    ranked = [eid for eid in hit_ids if eid in set(filtered)][: int(limit)]
                    if not ranked:
                        ranked = filtered[: int(limit)]
                except Exception:
                    ranked = filtered[: int(limit)]

            st.subheader("Top 落点（含源码预览 Top5）")
            for eid in ranked[: min(int(limit), 5)]:
                node = ctx.get_node(eid) or {}
                st.write(f"- {node.get('name') or eid} (`{eid}`)")
                st.code(ctx.get_code_snippet(eid), language=ctx.weaviate_data_svc.code_highlight_language())
            if len(ranked) > 5:
                st.caption(f"其余 {len(ranked) - 5} 个候选仅展示 id（避免页面过长）。")
                for eid in ranked[5: min(int(limit), 15)]:
                    st.write(f"- {eid}")


class WhyMethodBelongsScene(SceneGraphGuardsMixin):
    key = "scene_why_method_belongs"
    title = "为什么这段代码属于某业务域/能力？"

    def render(self, ctx: SceneTemplateContext) -> None:
        with st.container(border=True):
            st.markdown(f"### {self.title}")
            st.caption("方法 → IN_DOMAIN →（CONTAINS_CAPABILITY）的图谱路径，结合技术解读/源码做证据摘要。")

            method_input_key = f"{self.key}_method_input"
            domain_input_key = f"{self.key}_domain_input"
            capability_input_key = f"{self.key}_capability_input"

            with st.form("form_why_method_belongs", clear_on_submit=False):
                method_input = st.text_input(
                    "method_id（建议：从其它场景复制 `method://...`；或填方法名称会在图谱里模糊匹配）",
                    value="method://<id>",
                    key=method_input_key,
                )
                domain_input = st.text_input(
                    "domain_nid（可选：填域名称或 `domain://...`，例如 `后台管理域`）",
                    value="",
                    key=domain_input_key,
                )
                capability_input = st.text_input(
                    "capability_id（可选：填能力名称或 `capability://...`，例如 `商品管理`）",
                    value="",
                    key=capability_input_key,
                )
                run = st.form_submit_button("运行", type="primary")

            if not run:
                st.info("填写输入并点击「运行」。")
                return

            backend = self.require_memory_graph(
                ctx, purpose_cn="展示 IN_DOMAIN/CONTAINS_CAPABILITY 路径"
            )
            if backend is None:
                return

            method_id = str(method_input).strip()
            if not method_id.startswith("method://"):
                resolved = ctx.resolve_method_id(method_id)
                if not resolved:
                    st.warning("未能解析方法输入，请使用 `method://...` 或更准确的方法名称。")
                    return
                method_id = resolved

            wanted_domains: set[str] = set()
            if str(domain_input).strip():
                wanted_domains.add(ctx.domain_nid(str(domain_input).strip()))

            wanted_caps: set[str] = set()
            if str(capability_input).strip():
                wanted_caps.add(ctx.capability_nid(str(capability_input).strip()))

            # method -> domains
            domains: list[str] = []
            try:
                domains = backend.successors(method_id, rel_type="IN_DOMAIN")
            except Exception:
                domains = []

            if wanted_domains:
                domains = [d for d in domains if d in wanted_domains]

            if not domains:
                st.warning("未匹配到目标业务域（或图谱缺失 IN_DOMAIN 关系）。")
                st.write("该方法的 IN_DOMAIN 可能为空或图谱尚未同步。")
                return

            # domain -> capabilities
            caps: list[str] = []
            for did in domains:
                try:
                    caps.extend(backend.successors(did, rel_type="CONTAINS_CAPABILITY"))
                except Exception:
                    continue
            caps = _uniq_preserve_order(caps)
            if wanted_caps:
                caps = [c for c in caps if c in wanted_caps]

            st.subheader("证据链路（图谱）")
            st.write(f"方法：`{method_id}`")
            st.write("关联业务域：")
            st.write([ctx.get_node_name(did) for did in domains])

            if caps:
                st.write("上述业务域覆盖的能力：")
                st.write([ctx.get_node_name(cid) for cid in caps])
            else:
                st.caption("未匹配到能力节点（或 CONTAINS_CAPABILITY 缺失）。")

            st.subheader("技术解读（可作为自然语言证据）")
            tech = ctx.weaviate_data_svc.fetch_method_interpretation(method_id)
            if tech and tech.get("interpretation_text"):
                st.write(tech.get("interpretation_text"))
            else:
                st.caption("暂无技术解读文本（可先用源码+路径做解释）。")

            st.subheader("源码片段")
            st.code(ctx.get_code_snippet(method_id), language=ctx.weaviate_data_svc.code_highlight_language())


class KHopSemanticNeighborhoodScene(SceneGraphGuardsMixin):
    key = "scene_khop_neighborhood"
    title = "图谱结构浏览：围绕某节点的语义邻居摘要"

    def render(self, ctx: SceneTemplateContext) -> None:
        with st.container(border=True):
            st.markdown(f"### {self.title}")
            st.caption("从 entity_id 出发做 k-hop 邻居展开（可按 rel_type 过滤），再展示节点实体类型与名称。")

            # 起点支持两种方式：
            # 1) method：A-Z 分区下拉选择（方法签名+类名展示）或手动输入 method_id
            # 2) 其它：手动输入 entity_id
            start_kind = st.radio(
                "起点类型",
                options=["method（方法）", "domain（业务域）", "自定义 entity_id"],
                index=0,
                horizontal=True,
                key=f"{self.key}_start_kind",
            )

            method_picker_id: str | None = None
            method_manual_id_key = f"{self.key}_method_manual_id"
            method_manual_id = "method://<id>"
            if start_kind == "method（方法）":
                method_picker_id = render_az_method_picker(
                    ctx=ctx,
                    key_prefix=f"{self.key}_method_picker",
                    limit=2000,
                )
                method_manual_id = st.text_input(
                    "method_id（method://...，用于手动输入时使用）",
                    value=st.session_state.get(method_manual_id_key, "method://<id>"),
                    key=method_manual_id_key,
                )

            entity_input_key = f"{self.key}_entity_input"
            entity_input_default = "domain://<id>" if start_kind == "domain（业务域）" else "method://<id>"
            entity_input = None
            if start_kind != "method（方法）":
                entity_input = st.text_input(
                    "entity_id（如 method://... 或 domain://...）",
                    value=st.session_state.get(entity_input_key, entity_input_default),
                    key=entity_input_key,
                )

            with st.form("form_khop_neighborhood", clear_on_submit=False):
                k = st.slider("k-hop 深度", min_value=1, max_value=5, value=2, step=1)
                rel_type = st.text_input("rel_type 过滤（可空：不限制，如 calls/IN_DOMAIN/RELATED_TO）", value="")
                run = st.form_submit_button("运行", type="primary")

            if not run:
                st.info("填写输入并点击「运行」。")
                return

            backend = self.require_memory_graph(ctx, purpose_cn="计算邻居")
            if backend is None:
                return

            if start_kind == "method（方法）":
                start = str((method_picker_id or method_manual_id)).strip()
            else:
                start = str(entity_input).strip() if entity_input else ""
            rel_filter = str(rel_type).strip() or None

            # 双向 k-hop：successors + predecessors
            frontier = {start}
            all_nodes = {start}
            dist = {start: 0}

            for depth in range(int(k)):
                next_frontier: set[str] = set()
                for nid in list(frontier):
                    try:
                        succ = backend.successors(nid, rel_type=rel_filter) if rel_filter else backend.successors(nid)
                    except Exception:
                        succ = []
                    try:
                        pred = backend.predecessors(nid, rel_type=rel_filter) if rel_filter else backend.predecessors(nid)
                    except Exception:
                        pred = []
                    for nx in succ + pred:
                        if nx and nx not in all_nodes:
                            all_nodes.add(nx)
                            dist[nx] = depth + 1
                            next_frontier.add(nx)
                frontier = next_frontier
                if not frontier:
                    break

            # 展示
            st.subheader("邻居节点摘要")
            st.write(f"起点：`{start}`")
            st.write(f"总节点数：{len(all_nodes)}")

            # 限制展示，避免页面太长
            node_ids = list(all_nodes)
            node_ids.sort(key=lambda x: dist.get(x, 999))
            node_ids = node_ids[:80]

            for nid in node_ids:
                node = ctx.get_node(nid) or {}
                name = node.get("name") or nid
                st.write(f"- dist={dist.get(nid)} | {node.get('entity_type')}: {name}")

