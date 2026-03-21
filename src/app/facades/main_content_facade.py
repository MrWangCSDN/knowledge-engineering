from __future__ import annotations

from typing import Any, Callable

import streamlit as st

from src.app.i18n.ui_strings import get_ui_strings
from src.app.components.step_navigator import StepNavigator
from src.app.ui.display_theme import DISPLAY_THEME
from src.app.ui.streamlit_keys import SessionKeys
from src.app.utils.ontology_labels import OntologyLabels
from src.app.views.business_overview_view import BusinessOverviewView
from src.app.views.pattern_recognition_view import PatternRecognitionView
from src.app.views.owl_reasoning_view import OwlReasoningView
from src.app.views.business_domain_center_graph_view import BusinessDomainCenterGraphView
from src.app.facades.search_impact_facade import SearchImpactFacade
from src.app.views.scene_template_room_view import SceneTemplateRoomView
from src.app.services.app_services import AppServices
from src.core.paths import ui_knowledge_snapshot_dir
from src.core.domain_enums import InterpretPhase

from src.knowledge import KnowledgeGraph
from src.models.structure import EntityType, RelationType
from src.app.utils.node_utils import format_node_display_label


class MainContentFacade:
    """主内容区（步骤导航 + 步骤 1~5 渲染）以及解读进度自动刷新。"""

    def __init__(
        self,
        *,
        root_path,
        services: AppServices,
    ):
        self._root = root_path
        self._services = services

        self._graph: Any = None
        self._neo4j_fallback: Any = None

    def _render_interpret_progress(self) -> None:
        _mc = (get_ui_strings().get("main_content") or {}) if isinstance(get_ui_strings(), dict) else {}
        pl = self._services.pipeline_live
        if pl.get("_error_tb"):
            st.error(str(_mc.get("interpret_error_title") or "解读过程发生异常，请查看下方堆栈"))
            st.code(pl["_error_tb"], language="text")
        from src.app.components.pipeline_progress import PipelineProgressRenderer

        PipelineProgressRenderer.render(
            status=pl.get("status")
            or str(_mc.get("status_interpret_running") or "解读运行中…"),
            progress_frac=pl.get("progress_frac"),
            progress_md=pl.get("progress_md"),
            progress_label=pl.get("progress_label"),
            stats_md=pl.get("stats_md"),
            checklist_tech=pl.get("checklist_tech") or [],
            checklist_biz=pl.get("checklist_biz") or [],
            interp_stats=pl.get("interp_stats"),
            steps=pl.get("steps") or [],
            progress_source=pl.get("progress_source"),
            llm_backend_info=pl.get("llm_backend_info"),
            interpret_current_tech=pl.get("interpret_current_tech"),
            interpret_current_biz=pl.get("interpret_current_biz"),
            show_divider=True,
            show_refresh_button=True,
        )

    def _render_progress_auto_refresh_if_needed(self) -> None:
        _pp = (get_ui_strings().get("pipeline_progress") or {}) if isinstance(get_ui_strings(), dict) else {}
        pl = self._services.pipeline_live
        _is_pipeline_running = (
            st.session_state.get(SessionKeys.INTERPRET_PIPELINE_RUNNING)
            or st.session_state.get(SessionKeys.FULL_PIPELINE_RUNNING)
            or pl.get("running")
        )
        # Streamlit 运行时有时会因为 fragment “动态出现/消失”导致客户端刷新请求找不到对应 fragment。
        # 这里让 fragment 始终存在，仅在流水线运行时才真正渲染内容，避免重复报错。
        _sec = DISPLAY_THEME.fragment_refresh_interval_sec

        @st.fragment(run_every=_sec)
        def _progress_auto_refresh():
            if not _is_pipeline_running:
                return
            st.caption(
                str(_pp.get("auto_refresh_caption") or "💡 进度每 {seconds} 秒自动刷新").format(
                    seconds=_sec
                )
            )
            self._render_interpret_progress()

        _progress_auto_refresh()

    def _render_last_pipeline_progress_if_any(self) -> None:
        """当流水线未运行时，在主面板顶部渲染上次构建进度（含文件列表/步骤过程）。"""
        pl = self._services.pipeline_live
        _S = get_ui_strings() if isinstance(get_ui_strings(), dict) else {}
        _sb = (_S.get("sidebar") or {}) if isinstance(_S, dict) else {}
        _is_pipeline_running = (
            st.session_state.get(SessionKeys.INTERPRET_PIPELINE_RUNNING)
            or st.session_state.get(SessionKeys.FULL_PIPELINE_RUNNING)
            or pl.get("running")
        )
        if _is_pipeline_running:
            return
        if not st.session_state.get(SessionKeys.PIPELINE_LAST_STEPS):
            return

        from src.app.components.pipeline_progress import PipelineProgressRenderer

        # 尝试从 progress_md 里提取百分比：优先通过 progress_frac 走方块样式。
        progress_md = st.session_state.get(SessionKeys.PIPELINE_LAST_PROGRESS_MD) or ""
        progress_frac = None
        try:
            import re

            # 兼容：`... 100%` 或纯文本 `100%`
            m = re.search(r"(\\d+(?:\\.\\d+)?)\\s*%", progress_md)
            if m:
                pct = float(m.group(1))
                progress_frac = max(0.0, min(1.0, pct / 100.0))
        except Exception:
            progress_frac = None

        interp_stats = st.session_state.get(SessionKeys.PIPELINE_LAST_INTERP_STATS) or {}
        has_dual_phase = (
            isinstance(interp_stats, dict)
            and InterpretPhase.TECH.value in interp_stats
            and InterpretPhase.BIZ.value in interp_stats
        )

        stats_md = "" if has_dual_phase else (st.session_state.get(SessionKeys.PIPELINE_LAST_STATS_MD) or "")
        checklist_md = "" if has_dual_phase else (st.session_state.get(SessionKeys.PIPELINE_LAST_CHECKLIST_MD) or "")
        progress_md_to_use = "" if has_dual_phase else progress_md

        PipelineProgressRenderer.render(
            status=st.session_state.get(SessionKeys.PIPELINE_LAST_STATUS) or str(_sb.get("status_default_done") or "完成"),
            progress_frac=progress_frac,
            progress_md=progress_md_to_use,
            stats_md=stats_md,
            checklist_md=checklist_md,
            steps=st.session_state.get(SessionKeys.PIPELINE_LAST_STEPS) or [],
            header_caption=str(_sb.get("last_pipeline_caption") or "上次流水线（进度与工序已保留）"),
            interp_stats=interp_stats if has_dual_phase else None,
            show_divider=False,
            show_refresh_button=False,
        )

    def _load_graph(self, *, allow_snapshot_recovery: bool = True) -> None:
        # 优先使用当前进程中的内存图；若不存在，则（可选）尝试从上一次的快照恢复。
        # allow_snapshot_recovery 主要用于 step1/进度展示，避免触发慢路径。
        graph = self._services.get_graph_optional()
        if graph is None and allow_snapshot_recovery:
            # UI 专用快照目录：避免与命令行 out 混用
            ui_snap_dir = ui_knowledge_snapshot_dir(self._root)
            try:
                if ui_snap_dir.is_dir() and (ui_snap_dir / "graph.json").exists():
                    g = KnowledgeGraph()
                    self._services.snapshot_repo.load(g, ui_snap_dir)
                    self._services.set_global_graph(g)
                    graph = g
            except Exception:
                graph = self._services.get_graph_optional()

        self._graph = graph
        self._neo4j_fallback = self._services.get_neo4j_backend_optional() if graph is None else None

        # 当内存图未加载时，用 Neo4j 兜底
        _mc = (get_ui_strings().get("main_content") or {}) if isinstance(get_ui_strings(), dict) else {}
        if graph is None and self._neo4j_fallback is None:
            st.info(
                str(
                    _mc.get("empty_graph_hint")
                    or "👆 请先在侧边栏选择配置文件并点击「运行流水线」构建知识图谱；或配置 Neo4j（knowledge.graph.backend: neo4j）后刷新页面，将使用 Neo4j 数据。"
                )
            )
        elif graph is None:
            st.info(
                str(
                    _mc.get("neo4j_only_hint")
                    or "当前使用 Neo4j 数据（内存图未加载）。如需使用最新构建结果，可在侧边栏重新运行流水线。"
                )
            )

    def _data_backend(self):
        """供各步骤使用：优先内存图，否则 Neo4j 兜底。"""
        if self._graph is not None:
            return self._graph, None
        if self._neo4j_fallback is not None:
            return None, self._neo4j_fallback
        return None, None

    def _step_detail_container(self):
        return st.container()

    def _render_step1(self) -> None:
        _mc = (get_ui_strings().get("main_content") or {}) if isinstance(get_ui_strings(), dict) else {}
        st.subheader(str(_mc.get("step1_title") or "① 接入代码 & 构建图谱"))
        with self._step_detail_container():
            col_cfg, col_state = st.columns(2)
            with col_cfg:
                st.markdown(str(_mc.get("step1_config_heading") or "**当前配置**"))
                try:
                    cfg_path = self._root / (
                        st.session_state.get(SessionKeys.CONFIG_PATH) or "config/project.yaml"
                    )
                    if cfg_path.exists():
                        cfg_preview = self._services.load_config_fn(str(cfg_path))
                        repo_cfg = cfg_preview.repo
                        st.write(f"- 配置文件: `{cfg_path}`")
                        st.write(f"- 仓库路径: `{repo_cfg.path}`")
                        st.write(f"- 版本: `{repo_cfg.version or '未指定'}`")
                    else:
                        st.write("配置文件暂不可用（路径不存在）。")
                except Exception:
                    st.write("无法预览配置。")
            with col_state:
                st.markdown(str(_mc.get("step1_state_heading") or "**图谱状态**"))
                if self._graph is not None:
                    st.success(f"已加载内存图：节点 {self._graph.node_count()}，边 {self._graph.edge_count()}")
                    st.caption("本次会话中可直接用于检索、影响分析与 OWL 推理增强。")
                elif self._neo4j_fallback is not None:
                    # 只陈述“当前数据源”，避免暗示用户“明明运行过解读却仍未构建图谱”的歧义。
                    st.caption("图谱后端：Neo4j（内存图未加载）。本页将使用 Neo4j 数据进行浏览/检索。")
                else:
                    st.warning("尚未构建图谱，请先在侧边栏运行流水线，或在配置中启用 Neo4j 后刷新页面。")

    def _render_step2(self) -> None:
        st.subheader("② 图谱统计 / 系统结构")
        with self._step_detail_container():
            g2, backend2 = self._data_backend()
            if g2 is None and backend2 is None:
                st.caption("请先运行流水线或配置 Neo4j 后再查看统计。")
                return

            if g2 is not None:
                nodes = g2.node_count()
                edges = g2.edge_count()
            else:
                nodes = backend2.node_count()
                edges = backend2.edge_count()

            st.metric("节点数", nodes)
            st.metric("边数", edges)

            st.markdown("##### 按实体类型统计")
            if g2 is not None:
                graph_sig = f"nodes={nodes}|edges={edges}"
                cached_sig = st.session_state.get(SessionKeys.STEP2_TYPE_COUNTS_CACHE_GRAPH_SIG)
                rows = st.session_state.get(SessionKeys.STEP2_TYPE_COUNTS_ROWS_CACHE) if cached_sig == graph_sig else None

                if rows is None:
                    type_counts: dict[str, int] = {}
                    for nid in g2._g.nodes:
                        et = (g2._g.nodes[nid].get("entity_type") or "").lower()
                        if not et:
                            continue
                        type_counts[et] = type_counts.get(et, 0) + 1
                    rows = [{"实体类型": k, "数量": v} for k, v in sorted(type_counts.items(), key=lambda kv: kv[0])]
                    st.session_state[SessionKeys.STEP2_TYPE_COUNTS_ROWS_CACHE] = rows
                    st.session_state[SessionKeys.STEP2_TYPE_COUNTS_CACHE_GRAPH_SIG] = graph_sig

                if rows:
                    st.dataframe(rows, use_container_width=True, hide_index=True)
            else:
                st.caption("当前为 Neo4j 数据源，可在下方按本体浏览实体。")

            st.markdown("##### 本体 Schema（实体类型与关系类型）")
            col_et, col_rt = st.columns(2)
            with col_et:
                st.markdown("**实体类型**")
                entity_rows = [{"实体类型": et.value, "说明": OntologyLabels.entity_type_desc(et)} for et in EntityType]
                st.dataframe(entity_rows, use_container_width=True, hide_index=True)
            with col_rt:
                st.markdown("**关系类型**")
                rel_rows = [{"关系类型": rt.value, "说明": OntologyLabels.relation_type_desc(rt)} for rt in RelationType]
                st.dataframe(rel_rows, use_container_width=True, hide_index=True)

    def _render_step3(self) -> None:
        neo4j_fallback = self._neo4j_fallback
        facade = SearchImpactFacade(
            get_data_backend=self._data_backend,
            get_weaviate_svc=lambda: self._services.weaviate_data_svc,
            format_node_label=format_node_display_label,
            get_neo4j_backend_optional=self._services.get_neo4j_backend_optional,
        )
        facade.render_step3(neo4j_fallback=neo4j_fallback)

    def _render_step4(self) -> None:
        from src.app.components.ontology_browser import OntologyBrowser

        _mc = (get_ui_strings().get("main_content") or {}) if isinstance(get_ui_strings(), dict) else {}
        st.subheader(str(_mc.get("step4_title") or "④ 智能推理：补全间接依赖"))
        with self._step_detail_container():
            st.markdown("##### 图谱类型说明")
            col_et_s4, col_rt_s4 = st.columns(2)
            with col_et_s4:
                st.markdown("**实体类型**")
                entity_rows_s4 = [{"实体类型": et.value, "说明": OntologyLabels.entity_type_desc(et)} for et in EntityType]
                st.dataframe(entity_rows_s4, use_container_width=True, hide_index=True)
            with col_rt_s4:
                st.markdown("**关系类型**")
                rel_rows_s4 = [{"关系类型": rt.value, "说明": OntologyLabels.relation_type_desc(rt)} for rt in RelationType]
                st.dataframe(rel_rows_s4, use_container_width=True, hide_index=True)

            st.markdown("<div style='margin-top:1.2rem;'></div>", unsafe_allow_html=True)
            st.markdown("#### 按类型浏览图谱")
            st.caption("包 / 服务 / API 端点用下拉或二级联动；类、方法等按 A–Z 分区并支持关键词筛选，选中即可查看详情与关系。")

            neo4j_fallback = self._neo4j_fallback
            neo4j_s4 = neo4j_fallback if neo4j_fallback is not None else self._services.get_neo4j_backend_optional()
            close_s4 = neo4j_s4 is not None and neo4j_s4 is not neo4j_fallback
            if neo4j_s4 is None:
                st.warning("未配置 Neo4j 或未启用 Neo4j 后端。按实体类型浏览需要 Neo4j。")
            else:
                try:
                    browser_s4 = OntologyBrowser(neo4j_s4, "s4_ontology", self._services.weaviate_data_svc)
                    browser_s4.render()
                finally:
                    if close_s4 and neo4j_s4 is not None:
                        neo4j_s4.close()

            st.divider()
            st.markdown("##### 为什么需要「补全间接依赖」？")
            st.info(
                "代码里常有「A 调 B、B 调 C」的链式关系，但图谱里往往只存了直接边。"
                "**智能推理**会按规则自动补上「A 间接依赖 C」，让影响分析和「改一处会波及谁」的结论更完整、不遗漏。"
            )
            with st.expander("补全后能带来什么？", expanded=True):
                st.markdown(
                    "1. **影响分析更完整**  \n"
                    "   只按直接调用/继承分析会漏掉间接依赖。补全后，在 **④ 评估改动影响** 或本页 **试试看** 里，能看到更多下游/上游节点。\n\n"
                    "2. **依赖链一目了然**  \n"
                    "   继承链、实现链、服务调用链等会自动补上「跨多跳」的边，便于统计和后续查询。\n\n"
                    "3. **与评估步骤联动**  \n"
                    "   补全并写回图谱后，在 **④ 评估改动影响** 里选择「含推断边」，同一实体的影响范围会明显更大，可直接对比前后差异。"
                )
            st.caption("依赖补全基于当前知识图谱（内存图或 Neo4j）。若未安装推理组件，请在项目根目录执行：`pip install -e \".[owl]\"`。")

            with st.expander("使用指南", expanded=False):
                st.markdown(
                    "**操作步骤**  \n"
                    "1. 确保已运行流水线或配置 Neo4j，本页有图数据。  \n"
                    "2. 勾选「把补全结果写回图谱」后，点击 **开始补全**，等待完成。  \n"
                    "3. 本页会刷新 **补全效果一览**：边数变化、按实体的柱状图与对比卡片。  \n"
                    "4. 在卡片中可展开某行查看「因补全新增的可达节点」，或点 **用此实体对比** 在本页上方看该实体的完整对比。  \n\n"
                    "**你会看到**  \n"
                    "- 图中边数增加，从某些实体出发的可达节点数明显增多。  \n"
                    "- 柱状图里「补全后」高于「补全前」；做影响分析时能多看到一批间接依赖。  \n\n"
                    "**建议用法**  \n"
                    "- 日常评估「改某处会波及谁」：在 **④ 评估改动影响** 使用「含推断边」模式。  \n"
                    "- 本页用于：查看补全规模、哪些实体受益最多、以及具体新增了哪些间接依赖。  \n"
                )

            st.divider()
            OwlReasoningView(self._graph, self._neo4j_fallback, self._root).render()

    def _render_step5(self) -> None:
        _mc = (get_ui_strings().get("main_content") or {}) if isinstance(get_ui_strings(), dict) else {}
        st.subheader(str(_mc.get("step5_title") or "⑤ 模式识别：设计模式与架构模式"))
        with self._step_detail_container():
            PatternRecognitionView(load_config_fn=self._services.load_config_fn, services=self._services, root=self._root).render()

    def _render_step6(self) -> None:
        _mc = (get_ui_strings().get("main_content") or {}) if isinstance(get_ui_strings(), dict) else {}
        st.subheader(str(_mc.get("step6_title") or "⑥ 业务总览 / 模块视图"))
        with self._step_detail_container():
            g5, backend5 = self._data_backend()
            if g5 is None and backend5 is None:
                st.caption("请先运行流水线或配置 Neo4j 后再查看业务总览。")
                return
            BusinessDomainCenterGraphView(
                graph_backend=g5,
                neo4j_backend=backend5,
                services=self._services,
            ).render()
            BusinessOverviewView(self._services.load_config_fn, self._root).render()

    def render(self) -> None:
        self._render_progress_auto_refresh_if_needed()
        self._render_last_pipeline_progress_if_any()

        # 首页入口：在不改变 Step 体验的前提下，提供一个“场景样板间”Portal。
        home_mode_options = ("知识工程构建", "场景样板间")
        if SessionKeys.MAIN_HOME_MODE not in st.session_state or st.session_state.get(SessionKeys.MAIN_HOME_MODE) not in home_mode_options:
            st.session_state[SessionKeys.MAIN_HOME_MODE] = home_mode_options[0]

        st.divider()
        st.caption("选择一个演示模式：知识工程构建 或 场景样板间")
        home_mode = st.radio(
            "演示模式",
            options=["知识工程构建", "场景样板间"],
            index=0 if st.session_state[SessionKeys.MAIN_HOME_MODE] == "知识工程构建" else 1,
            horizontal=True,
            key=SessionKeys.MAIN_HOME_MODE,
            label_visibility="collapsed",
        )

        if home_mode == "场景样板间":
            st.divider()
            # 加载内存图；若有 Neo4j 配置则**始终**传入（与 _load_graph 中「有内存图则不连库」区分），
            # 否则「方法查表」等仅沿 calls 遍历的功能会落在缺边的内存图上，结果恒为空。
            self._load_graph(allow_snapshot_recovery=False)
            neo4j_for_room = self._neo4j_fallback
            close_extra_neo4j = False
            if self._graph is not None and neo4j_for_room is None:
                neo4j_for_room = self._services.get_neo4j_backend_optional()
                close_extra_neo4j = neo4j_for_room is not None
            try:
                SceneTemplateRoomView(
                    services=self._services,
                    graph_backend=self._graph,
                    neo4j_backend=neo4j_for_room,
                ).render()
            finally:
                if close_extra_neo4j and neo4j_for_room is not None:
                    neo4j_for_room.close()
                if self._neo4j_fallback is not None:
                    self._neo4j_fallback.close()
                    self._neo4j_fallback = None
            return

        # 走原有 Step 流程
        st.divider()
        step_nav = StepNavigator(session_key=SessionKeys.MAIN_STEP)
        step = step_nav.render()
        # 懒加载：仅在需要图谱/Neo4j 数据的步骤上加载，避免「仅运行解读」完成后多等几秒。
        if step in ("step1", "step2", "step3", "step4", "step6"):
            # step1 只需展示“图谱状态”，不必触发快照恢复的慢路径。
            self._load_graph(allow_snapshot_recovery=(step != "step1"))
        st.divider()

        render_map: dict[str, Callable[[], None]] = {
            "step1": self._render_step1,
            "step2": self._render_step2,
            "step3": self._render_step3,
            "step4": self._render_step4,
            "step5": self._render_step5,
            "step6": self._render_step6,
        }
        fn = render_map.get(step)
        if fn is not None:
            fn()

        if self._neo4j_fallback is not None:
            self._neo4j_fallback.close()

