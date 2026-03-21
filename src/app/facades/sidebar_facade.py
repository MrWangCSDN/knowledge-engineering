from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import html
import streamlit as st

from src.app.components.pipeline_progress import PipelineProgressRenderer
from src.app.i18n.ui_strings import get_ui_strings
from src.app.services.app_services import AppServices
from src.app.ui.display_theme import DISPLAY_THEME
from src.app.ui.streamlit_keys import SessionKeys
from src.core.domain_enums import InterpretPhase
from src.core.paths import structure_facts_interpret_cache_display_path, structure_facts_interpret_cache_path
from src.pipeline.gateways import load_project_config as default_load_config


class SidebarFacade:
    """侧边栏：配置输入、全量/仅解读流水线启动，以及上次进度展示。"""

    def __init__(
        self,
        *,
        root: Path,
        services: AppServices,
        load_config_fn: Callable[[str | Path], Any] = default_load_config,
    ):
        self._root = root
        self._services = services
        self._load_config_fn = load_config_fn

    def render(self) -> None:
        _S = get_ui_strings()
        _sb = (_S.get("sidebar") or {}) if isinstance(_S, dict) else {}

        with st.sidebar:
            _pipeline_live = self._services.pipeline_live

            def _fmt_phase(src: dict, phase: str) -> str:
                p = (src.get(phase) or {}) if isinstance(src, dict) else {}
                return f"{int(p.get('done', 0))}/{int(p.get('total', 0))}"

            def _fmt_live_phase(live_stats: dict, phase: str) -> str:
                v = live_stats.get(phase)
                if isinstance(v, (list, tuple)) and len(v) == 2:
                    return f"{int(v[0])}/{int(v[1])}"
                return "0/0"

            def _fmt_key_count(v: object) -> str:
                if v is None:
                    return str(_sb.get("key_count_not_queried") or "运行中未查询")
                return str(v)

            # 运行中进度、清单、工序步骤仅在主内容区展示（MainContentFacade + 自动刷新）
            if _pipeline_live.get("completed") and not _pipeline_live.get("running") and _pipeline_live.get("mode") in ("interpret_only", "full"):
                st.session_state[SessionKeys.PIPELINE_LAST_STEPS] = list(_pipeline_live.get("steps", []))
                st.session_state[SessionKeys.PIPELINE_LAST_STATUS] = _pipeline_live.get(
                    "status", _sb.get("status_default_done") or "完成"
                )
                st.session_state[SessionKeys.PIPELINE_LAST_PROGRESS_MD] = _pipeline_live.get("progress_md", "")
                _interp = _pipeline_live.get("_interp_stats", {})
                if _interp:
                    _sl = []
                    _td, _tt, _bd, _bt = 0, 0, 0, 0
                    if InterpretPhase.TECH.value in _interp:
                        d, t = _interp[InterpretPhase.TECH.value]
                        _td, _tt = d, t
                        _sl.append(
                            str(_sb.get("tech_progress_sidebar") or "技术解读进度：**{done}/{total}**").format(
                                done=d, total=t
                            )
                        )
                    if InterpretPhase.BIZ.value in _interp:
                        d, t = _interp[InterpretPhase.BIZ.value]
                        _bd, _bt = d, t
                        _sl.append(
                            str(_sb.get("biz_progress_sidebar") or "业务解读进度：**{done}/{total}**").format(
                                done=d, total=t
                            )
                        )
                    st.session_state[SessionKeys.PIPELINE_LAST_STATS_MD] = "  \n".join(_sl)
                    # 供主面板复用：渲染双阶段方块进度条（避免 progress_md 解析失败导致无方块）
                    _interp_stats: dict[str, Any] = {}
                    _interp_stats[InterpretPhase.TECH.value] = (_td, _tt)
                    _interp_stats[InterpretPhase.BIZ.value] = (_bd, _bt)
                    st.session_state[SessionKeys.PIPELINE_LAST_INTERP_STATS] = _interp_stats
                    self._services.interp_progress_svc.save(_pipeline_live.get("_sf_path", ""), _td, _tt, _bd, _bt)
                else:
                    st.session_state.pop(SessionKeys.PIPELINE_LAST_STATS_MD, None)
                    st.session_state.pop(SessionKeys.PIPELINE_LAST_INTERP_STATS, None)

                # 生成“上次流水线”时的清单 markdown。
                # 若某阶段已达到 done>=total，则不再展示对应“待解读清单”，避免 100% 仍刷出长列表。
                _cl_parts = []
                _tech_done_all = bool(_tt > 0 and _td >= _tt)
                _biz_done_all = bool(_bt > 0 and _bd >= _bt)

                if _pipeline_live.get("checklist_tech") and not _tech_done_all:
                    _safe = lambda x: html.escape(x).replace("`", "&#96;")
                    _cl_lines = [
                        f"- {'[x]' if d else '[ ]'} `{_safe(s)}`" for s, d in _pipeline_live["checklist_tech"]
                    ]
                    _cl_parts.append(
                        str(_sb.get("checklist_tech_pending_title") or "**技术解读清单（待解读）**")
                        + "  \n"
                        + "\n".join(_cl_lines)
                    )

                if _pipeline_live.get("checklist_biz") and not _biz_done_all:
                    _safe = lambda x: html.escape(x).replace("`", "&#96;")
                    _cl_lines = [
                        f"- {'[x]' if d else '[ ]'} `{_safe(s)}`" for s, d in _pipeline_live["checklist_biz"]
                    ]
                    _cl_parts.append(
                        str(_sb.get("checklist_biz_pending_title") or "**业务解读清单（待解读）**")
                        + "  \n"
                        + "\n".join(_cl_lines)
                    )

                if _cl_parts:
                    st.session_state[SessionKeys.PIPELINE_LAST_CHECKLIST_MD] = "  \n\n".join(_cl_parts)
                else:
                    st.session_state.pop(SessionKeys.PIPELINE_LAST_CHECKLIST_MD, None)

                if _pipeline_live.get("_success_msg"):
                    st.sidebar.success(_pipeline_live["_success_msg"])
                _pipeline_live["completed"] = False
                _pipeline_live["mode"] = ""
                st.session_state.pop(SessionKeys.INTERPRET_PIPELINE_RUNNING, None)
                st.session_state.pop(SessionKeys.FULL_PIPELINE_RUNNING, None)
                st.rerun()
            # 仅在主面板展示「上次流水线进度条」，避免重复分散在侧边栏

            st.header(str(_sb.get("header_config") or "配置与构建"))
            config_path = st.text_input(
                str(_sb.get("config_path_label") or "配置文件路径"),
                value="config/project.yaml",
                help=str(
                    _sb.get("config_path_help") or "相对于项目根的 YAML 路径，如 config/project.yaml"
                ),
                key=SessionKeys.CONFIG_PATH,
            )

            _pipe_yaml = Path(config_path)
            if not _pipe_yaml.is_absolute():
                _pipe_yaml = self._root / _pipe_yaml
            _def_interp = False
            _def_biz = False
            if _pipe_yaml.exists():
                try:
                    _cy = self._load_config_fn(str(_pipe_yaml))
                    _pipe = _cy.knowledge.pipeline
                    _def_interp = _pipe.include_method_interpretation_build
                    _def_biz = _pipe.include_business_interpretation_build
                except Exception:
                    pass

            include_method_interpretation = st.checkbox(
                str(_sb.get("checkbox_interpretation_label") or "本次构建包含方法技术解读（增量续跑）"),
                value=_def_interp,
                help=str(
                    _sb.get("checkbox_interpretation_help")
                    or "需启用 method_interpretation + vectordb-interpret。勾选后只对 Weaviate 中尚无记录的方法调 LLM；可配合 max_methods 分批多次跑完。"
                ),
                key=SessionKeys.SIDEBAR_PIPELINE_INCLUDE_INTERPRETATION,
            )
            include_business_interpretation = st.checkbox(
                str(_sb.get("checkbox_business_label") or "本次构建包含业务解读（类 / API / 模块，增量续跑）"),
                value=_def_biz,
                help=str(
                    _sb.get("checkbox_business_help")
                    or "需启用 business_interpretation + vectordb-business。勾选后只对 BusinessInterpretation 中尚无 (实体,level) 的记录调 LLM；可配合 max_classes/max_apis/max_modules 分批续跑。"
                ),
                key=SessionKeys.SIDEBAR_PIPELINE_INCLUDE_BUSINESS,
            )
            st.caption(
                str(
                    _sb.get("pipeline_options_caption")
                    or "两项勾选相互独立；均不勾选时本轮只更新图谱与代码向量。默认勾选状态来自 `knowledge.pipeline.include_*_build`。"
                )
            )

            if st.button(
                str(_sb.get("run_pipeline_button") or "🔄 运行流水线"),
                type="primary",
                use_container_width=True,
            ):
                cfg_path = Path(config_path)
                if not cfg_path.is_absolute():
                    cfg_path = self._root / cfg_path
                if not cfg_path.exists():
                    st.sidebar.error(f"配置文件不存在: {cfg_path}")
                else:
                    _sf_path_pipeline = str(self._services.structure_facts_repo.get_default_cache_path(cfg_path))
                    _pipeline_live["running"] = True
                    _pipeline_live["mode"] = "full"
                    _pipeline_live["status"] = str(_sb.get("status_pipeline_running") or "流水线构建中…")
                    _pipeline_live["progress_frac"] = 0.0
                    _pipeline_live["progress_label"] = str(_sb.get("progress_label_start") or "开始")
                    _pipeline_live["stats_md"] = ""
                    _pipeline_live["steps"] = []
                    _pipeline_live["checklist_tech"] = []
                    _pipeline_live["checklist_biz"] = []
                    _pipeline_live["interp_stats"] = {}
                    _pipeline_live["interpret_current_tech"] = None
                    _pipeline_live["interpret_current_biz"] = None
                    st.session_state[SessionKeys.FULL_PIPELINE_RUNNING] = True
                    self._services.pipeline_runner.run_full_pipeline(
                        cfg_path,
                        include_method_interpretation,
                        include_business_interpretation,
                        _sf_path_pipeline,
                    )
                    st.rerun()

            _sidebar_running = bool(
                _pipeline_live.get("running")
                or st.session_state.get(SessionKeys.INTERPRET_PIPELINE_RUNNING)
                or st.session_state.get(SessionKeys.FULL_PIPELINE_RUNNING)
            )
            with st.expander(
                str(_sb.get("interpret_expander_title") or "代码未变：仅运行解读（不重建图谱）"),
                expanded=_sidebar_running,
            ):
                _cap_tpl = str(
                    _sb.get("interpret_expander_caption")
                    or "不清理 Neo4j、不重建内存图、不写代码向量；只根据**已缓存的结构事实**调用 LLM，"
                    "做方法技术解读与/或业务解读（与完整流水线一样支持增量续跑）。缓存文件：`{cache_hint}`（完整流水线在**结构层完成后**即写入，"
                    "跑完全程会再覆盖一次；若从未跑过流水线或只用「到结构层为止」，需先至少跑到语义/知识层或自行指定 JSON）。"
                )
                st.caption(_cap_tpl.format(cache_hint=structure_facts_interpret_cache_display_path()))
                _cfg_sf = Path(config_path)
                if not _cfg_sf.is_absolute():
                    _cfg_sf = self._root / _cfg_sf

                _default_cache = (
                    self._services.structure_facts_repo.get_default_cache_path(_cfg_sf)
                    if _cfg_sf.exists()
                    else structure_facts_interpret_cache_path(self._root)
                )
                _sf_in = st.text_input(
                    str(_sb.get("structure_facts_input_label") or "结构事实 JSON 路径（可改）"),
                    value=str(_default_cache),
                    key=SessionKeys.INTERPRET_ONLY_STRUCTURE_FACTS_PATH,
                    help=str(
                        _sb.get("structure_facts_input_help")
                        or "默认与完整流水线写入位置一致；也可指向自行导出的 structure_facts.json"
                    ),
                )
                c_tech_only = st.checkbox(
                    str(_sb.get("interpret_tech_checkbox") or "执行方法技术解读"),
                    value=True,
                    key=SessionKeys.INTERPRET_ONLY_TECH,
                )
                c_biz_only = st.checkbox(
                    str(_sb.get("interpret_biz_checkbox") or "执行业务解读"),
                    value=True,
                    key=SessionKeys.INTERPRET_ONLY_BIZ,
                )
                _sf_for_hint = Path(
                    (
                        st.session_state.get(SessionKeys.INTERPRET_ONLY_STRUCTURE_FACTS_PATH) or ""
                    ).strip()
                    or str(_default_cache)
                )
                if _sf_for_hint.exists():
                    _show_diag = st.checkbox(
                        str(_sb.get("interpret_diag_checkbox") or "显示诊断信息（进度回退排查）"),
                        value=False,
                        key=SessionKeys.INTERPRET_ONLY_SHOW_DIAG,
                    )
                    if _show_diag:
                        _is_running = (
                            st.session_state.get(SessionKeys.INTERPRET_PIPELINE_RUNNING)
                            or st.session_state.get(SessionKeys.FULL_PIPELINE_RUNNING)
                            or _pipeline_live.get("running")
                        )

                        def _render_diag(diag: dict, live_stats: dict) -> None:
                            def _diag_stats_block(
                                title: str,
                                lines: list[str],
                                *,
                                top_margin: str,
                            ) -> str:
                                """标题与下方等宽统计块紧贴；top_margin 与上方控件留白。"""
                                # 使用 <br /> 强制三行（Streamlit 对 <pre> 内 \\n 有时会压成一行）
                                body = "<br />".join(html.escape(line) for line in lines)
                                esc_title = html.escape(title)
                                return (
                                    f'<div style="margin-top: {top_margin};">'
                                    f'<p style="margin: 0 0 0.12rem 0; padding: 0; font-size: 0.8125rem; '
                                    f'color: rgba(49, 51, 56, 0.62); line-height: 1.35;">{esc_title}</p>'
                                    f'<div style="margin: 0; padding: 0.6rem 0.7rem; '
                                    f'background-color: rgb(245, 245, 245); border-radius: 0.375rem; '
                                    f'font-family: Source Code Pro, ui-monospace, monospace; font-size: 0.8rem; '
                                    f'line-height: 1.55;">{body}</div></div>'
                                )

                            _pt = InterpretPhase.TECH.value
                            _pb = InterpretPhase.BIZ.value
                            _tech_lines = [
                                str(_sb.get("diag_line_memory") or "内存态:      {value}").format(
                                    value=_fmt_live_phase(live_stats, _pt)
                                ),
                                str(_sb.get("diag_line_weaviate") or "Weaviate态:  {value}").format(
                                    value=_fmt_phase(diag.get("weaviate", {}), _pt)
                                ),
                                str(_sb.get("diag_line_keys") or "Weaviate续跑key数: {value}").format(
                                    value=_fmt_key_count(diag.get("existing_method_ids_count"))
                                ),
                            ]
                            _biz_lines = [
                                str(_sb.get("diag_line_memory") or "内存态:      {value}").format(
                                    value=_fmt_live_phase(live_stats, _pb)
                                ),
                                str(_sb.get("diag_line_weaviate") or "Weaviate态:  {value}").format(
                                    value=_fmt_phase(diag.get("weaviate", {}), _pb)
                                ),
                                str(_sb.get("diag_line_keys") or "Weaviate续跑key数: {value}").format(
                                    value=_fmt_key_count(diag.get("existing_biz_key_pairs_count"))
                                ),
                            ]
                            st.markdown(
                                _diag_stats_block(
                                    str(_sb.get("diag_tech_title") or "技术解读： 已完成/总数"),
                                    _tech_lines,
                                    top_margin=DISPLAY_THEME.diag_stats_block_margin_top_first,
                                ),
                                unsafe_allow_html=True,
                            )
                            st.markdown(
                                _diag_stats_block(
                                    str(_sb.get("diag_biz_title") or "业务解读： 已完成/总数"),
                                    _biz_lines,
                                    top_margin=DISPLAY_THEME.diag_stats_block_margin_top_second,
                                ),
                                unsafe_allow_html=True,
                            )

                        if _is_running:

                            @st.fragment(run_every=DISPLAY_THEME.fragment_refresh_interval_sec)
                            def _diag_auto_refresh() -> None:
                                diag = self._services.interp_progress_svc.diagnose(
                                    str(_sf_for_hint),
                                    config_path,
                                    include_existing_keys=False,
                                )
                                live_stats = _pipeline_live.get("interp_stats") or {}
                                _render_diag(diag, live_stats)

                            _diag_auto_refresh()
                        else:
                            diag = self._services.interp_progress_svc.diagnose(str(_sf_for_hint), config_path)
                            live_stats = _pipeline_live.get("interp_stats") or {}
                            _render_diag(diag, live_stats)

                if st.button(
                    str(_sb.get("interpret_only_button") or "⚡ 仅运行解读"),
                    type="secondary",
                    use_container_width=True,
                    key=SessionKeys.BTN_INTERPRET_ONLY,
                ):
                    _cfg_run = Path(config_path)
                    if not _cfg_run.is_absolute():
                        _cfg_run = self._root / _cfg_run
                    if not _cfg_run.exists():
                        st.sidebar.error(f"配置文件不存在: {_cfg_run}")
                    else:
                        _sf = Path(
                            (
                                st.session_state.get(SessionKeys.INTERPRET_ONLY_STRUCTURE_FACTS_PATH)
                                or ""
                            ).strip()
                            or str(_default_cache)
                        )
                        if not _sf.exists():
                            st.sidebar.error(f"未找到结构事实文件，请先完整运行一次流水线生成缓存：\n{_sf}")
                        elif not c_tech_only and not c_biz_only:
                            st.sidebar.warning("请至少勾选一种解读。")
                        else:
                            _sf_path_io = str(Path(_sf).resolve())
                            _pipeline_live.pop("_error_tb", None)
                            _pipeline_live["running"] = True
                            _pipeline_live["mode"] = "interpret_only"
                            _pipeline_live["status"] = str(
                                _sb.get("status_interpret_running") or "解读运行中…"
                            )
                            _pipeline_live["progress_md"] = "开始  \n`[░░░░░░░░░░░░░░░░░░░░░] 0%`"
                            _pipeline_live["progress_frac"] = 0.0
                            _pipeline_live["progress_label"] = str(
                                _sb.get("progress_label_start") or "开始"
                            )
                            _pipeline_live["stats_md"] = ""
                            _pipeline_live["checklist_tech"] = []
                            _pipeline_live["checklist_biz"] = []
                            _pipeline_live["interp_stats"] = {}
                            _pipeline_live["interpret_current_tech"] = None
                            _pipeline_live["interpret_current_biz"] = None
                            _pipeline_live["steps"] = list(
                                self._services.pipeline_runner.interpret_skip_steps()
                            )
                            st.session_state[SessionKeys.INTERPRET_PIPELINE_RUNNING] = True
                            self._services.pipeline_runner.run_interpret_only(
                                _cfg_run,
                                _sf,
                                c_tech_only,
                                c_biz_only,
                                _sf_path_io,
                            )

