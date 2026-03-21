"""PipelineRunner 的 UI 回调与收尾逻辑：挂在 PipelineLiveCoordinator 上，便于单测与阅读。"""
from __future__ import annotations

import re
import time
import traceback
from pathlib import Path
from typing import Any, Callable

import streamlit as st

from src.app.services.pipeline_live_coordinator import PipelineLiveCoordinator
from src.app.ui.display_theme import DISPLAY_THEME
from src.app.ui.streamlit_keys import SessionKeys
from src.core.domain_enums import InterpretPhase
from src.core.paths import ui_knowledge_snapshot_dir
from src.persistence.repositories import InterpretationProgressRepository, SnapshotRepository

_INTERP_TECH = InterpretPhase.TECH.value
_INTERP_BIZ = InterpretPhase.BIZ.value


def normalize_checklist_rows(items: Any) -> list[tuple[str, bool]]:
    rows: list[tuple[str, bool]] = []
    for it in items or []:
        if isinstance(it, (list, tuple)) and len(it) >= 2:
            rows.append((str(it[0]), bool(it[1])))
        else:
            rows.append((str(it), False))
    return rows


class FullPipelineUiSubscription:
    """全量流水线：步骤 / 解读统计 / checklist / 进度条与成功收尾。"""

    def __init__(
        self,
        *,
        coordinator: PipelineLiveCoordinator,
        pl: dict[str, Any],
        interp_svc: InterpretationProgressRepository,
        sf_path_pipeline: str,
        get_graph: Callable[[], Any],
        snapshot_repo: SnapshotRepository,
        root: Path,
    ) -> None:
        self._coord = coordinator
        self._pl = pl
        self._interp_svc = interp_svc
        self._sf_path_pipeline = sf_path_pipeline
        self._get_graph = get_graph
        self._snapshot_repo = snapshot_repo
        self._root = root
        self._step_lines: list[str] = []
        self._interp_stats_display: dict[str, tuple[int, int]] = {}
        self._used_persisted: set[str] = set()
        self._last_progress_persist_at = 0.0
        self._progress_persist_interval_sec = 2.0

    def _emit(self, event_type: str, **payload: Any) -> None:
        self._coord.emit(event_type, **payload)  # type: ignore[arg-type]

    def on_step(self, msg: str) -> None:
        self._step_lines.append(msg)
        self._emit("steps", set_steps=list(self._step_lines))
        if "请求后端" in msg and "实际使用" in msg:
            self._emit(
                "meta",
                llm_backend_info=msg.replace("技术解读：", "").replace("业务解读：", "").strip(),
            )

    def on_interpretation_stats(self, already_done: int, total: int, phase: InterpretPhase) -> None:
        pk = phase.value
        persisted = self._interp_svc.load(self._sf_path_pipeline)
        if already_done == 0 and persisted.get(pk, {}).get("done", 0) > 0:
            pd = persisted[pk]["done"]
            pt = persisted[pk].get("total", total)
            self._interp_stats_display[pk] = (pd, pt)
            self._used_persisted.add(pk)
        else:
            self._interp_stats_display[pk] = (already_done, total)
            self._used_persisted.discard(pk)
        self._emit("meta", interp_stats=dict(self._interp_stats_display))
        lines = []
        if _INTERP_TECH in self._interp_stats_display:
            d, t = self._interp_stats_display[_INTERP_TECH]
            s = "（来自上次运行记录）" if _INTERP_TECH in self._used_persisted else ""
            lines.append(f"技术解读进度：**{d}/{t}**{s}")
        if _INTERP_BIZ in self._interp_stats_display:
            d, t = self._interp_stats_display[_INTERP_BIZ]
            s = "（来自上次运行记录）" if _INTERP_BIZ in self._used_persisted else ""
            lines.append(f"业务解读进度：**{d}/{t}**{s}")
        self._emit("meta", stats_md="  \n".join(lines) if lines else "")
        if _INTERP_TECH in self._interp_stats_display:
            d, t = self._interp_stats_display[_INTERP_TECH]
            self._emit(
                "progress",
                progress_frac=min(d / t, 1.0) if t else 0.0,
                progress_label=f"技术解读进度：{d}/{t}",
            )
        elif _INTERP_BIZ in self._interp_stats_display:
            d, t = self._interp_stats_display[_INTERP_BIZ]
            self._emit(
                "progress",
                progress_frac=min(d / t, 1.0) if t else 0.0,
                progress_label=f"业务解读进度：{d}/{t}",
            )

    def on_item_list(self, items: Any) -> None:
        rows = normalize_checklist_rows(items)
        if _INTERP_BIZ in self._interp_stats_display:
            self._emit("meta", checklist_biz=rows, interpret_current_biz=None)
        else:
            self._emit("meta", checklist_tech=rows, interpret_current_tech=None)

    def on_item_started(self, label: str, phase: InterpretPhase) -> None:
        if phase == InterpretPhase.BIZ:
            self._emit("meta", interpret_current_biz=label)
        else:
            self._emit("meta", interpret_current_tech=label)

    def on_item_completed(self, sig: str, done: bool) -> None:
        pl = self._pl
        matched_key: str | None = None
        for lst, key in [
            (pl.get("checklist_tech") or [], "checklist_tech"),
            (pl.get("checklist_biz") or [], "checklist_biz"),
        ]:
            for i, (s, _) in enumerate(lst):
                if s == sig:
                    lst[i] = (s, done)
                    self._emit("meta", **{key: list(lst)})
                    matched_key = key
                    break
            if matched_key:
                break
        if matched_key == "checklist_biz":
            self._emit("meta", interpret_current_biz=None)
        elif matched_key == "checklist_tech":
            self._emit("meta", interpret_current_tech=None)
        if done and self._interp_stats_display:
            if _INTERP_BIZ in self._interp_stats_display:
                d, t = self._interp_stats_display[_INTERP_BIZ]
                self._interp_stats_display[_INTERP_BIZ] = (d + 1, t)
            elif _INTERP_TECH in self._interp_stats_display:
                d, t = self._interp_stats_display[_INTERP_TECH]
                self._interp_stats_display[_INTERP_TECH] = (d + 1, t)
            self._emit("meta", interp_stats=dict(self._interp_stats_display))
            lines = []
            if _INTERP_TECH in self._interp_stats_display:
                d, t = self._interp_stats_display[_INTERP_TECH]
                lines.append(f"技术解读进度：**{d}/{t}**")
            if _INTERP_BIZ in self._interp_stats_display:
                d, t = self._interp_stats_display[_INTERP_BIZ]
                lines.append(f"业务解读进度：**{d}/{t}**")
            self._emit("meta", stats_md="  \n".join(lines))
            if _INTERP_TECH in self._interp_stats_display:
                d, t = self._interp_stats_display[_INTERP_TECH]
                self._emit(
                    "progress",
                    progress_frac=min(d / t, 1.0) if t else 0.0,
                    progress_label=f"技术解读进度：{d}/{t}",
                )
            elif _INTERP_BIZ in self._interp_stats_display:
                d, t = self._interp_stats_display[_INTERP_BIZ]
                self._emit(
                    "progress",
                    progress_frac=min(d / t, 1.0) if t else 0.0,
                    progress_label=f"业务解读进度：{d}/{t}",
                )

            now = time.monotonic()
            if now - self._last_progress_persist_at >= self._progress_persist_interval_sec:
                self._last_progress_persist_at = now
                try:
                    tech_done, tech_total = self._interp_stats_display.get(_INTERP_TECH, (0, 0))
                    biz_done, biz_total = self._interp_stats_display.get(_INTERP_BIZ, (0, 0))
                    self._interp_svc.save(
                        self._sf_path_pipeline,
                        tech_done,
                        tech_total,
                        biz_done,
                        biz_total,
                    )
                except Exception:
                    pass

    def on_pipeline_progress(self, current: int, total: int, message: str) -> None:
        self._emit("status", status=message or "运行中…")
        if total and total > 0:
            self._emit(
                "progress",
                progress_frac=min(current / total, 1.0),
                progress_label=f"进度：{current}/{total}",
            )
        else:
            self._emit("progress", progress_frac=0.0, progress_label="进度")

    def finalize_success(
        self,
        *,
        result: dict[str, Any],
        cfg: dict[str, Any],
        include_method_interpretation: bool,
        include_business_interpretation: bool,
    ) -> None:
        use_neo4j = ((cfg.get("knowledge") or {}).get("graph") or {}).get("backend") == "neo4j"
        neo4j_status = result.get("neo4j_sync")
        interp_r = result.get("interpretation") or {}
        biz_r = result.get("business_interpretation") or {}
        _interp_done = bool(
            include_method_interpretation
            and interp_r.get("mode") not in ("graph_and_code_only", "interpret_config_disabled")
        )
        _biz_done = bool(
            include_business_interpretation
            and not biz_r.get("skipped")
            and (biz_r.get("written", 0) > 0 or biz_r.get("total_targets", 0) > 0)
        )
        _llm_hint = []
        if _interp_done:
            _llm_hint.append("方法技术解读")
        if _biz_done:
            _llm_hint.append("业务解读")
        _llm_suffix = ("（含 " + "、".join(_llm_hint) + "）") if _llm_hint else ""

        self._emit("status", status="流水线构建完成" + _llm_suffix)
        if interp_r.get("mode") == "graph_and_code_only":
            self._emit(
                "status",
                status="构建完成（图谱与代码向量已更新，方法技术解读库已保留）"
                + (_llm_suffix if _biz_done else ""),
            )
        elif use_neo4j and neo4j_status == "ok":
            self._emit("status", status="Neo4j 同步完成" + _llm_suffix)
        self._emit("progress", progress_frac=1.0, progress_label="完成")
        _n_done = DISPLAY_THEME.full_pipeline_complete_bar_segments
        self._emit("progress", progress_md="完成  \n`[" + "█" * _n_done + "] 100%`")
        self._emit("meta", _result=result)
        self._emit("steps", set_steps=list(self._step_lines))

        disp = self._interp_stats_display
        if disp:
            _td, _tt, _bd, _bt = 0, 0, 0, 0
            if _INTERP_TECH in disp:
                d, t = disp[_INTERP_TECH]
                _td, _tt = d, t
            if _INTERP_BIZ in disp:
                d, t = disp[_INTERP_BIZ]
                _bd, _bt = d, t
            self._interp_svc.save(self._sf_path_pipeline, _td, _tt, _bd, _bt)

        if disp:
            _sl = []
            if _INTERP_TECH in disp:
                d, t = disp[_INTERP_TECH]
                _sl.append(f"技术解读进度：**{d}/{t}**")
            if _INTERP_BIZ in disp:
                d, t = disp[_INTERP_BIZ]
                _sl.append(f"业务解读进度：**{d}/{t}**")
            self._emit("meta", stats_md="  \n".join(_sl))

        if SessionKeys.OWL_LAST_RESULT in st.session_state:
            del st.session_state[SessionKeys.OWL_LAST_RESULT]
        try:
            current_graph = self._get_graph()
            if current_graph is not None:
                ui_snap_dir = ui_knowledge_snapshot_dir(self._root)
                repo_cfg = (cfg.get("repo") or {}) if isinstance(cfg, dict) else {}
                version = repo_cfg.get("version") or "default_ui"
                self._snapshot_repo.save(current_graph, ui_snap_dir, version=version)
        except Exception:
            pass

        interp = result.get("interpretation") or {}
        biz = result.get("business_interpretation") or {}
        _msg = result.get("message", "")
        if interp.get("mode") == "graph_and_code_only":
            interp_tail = "；方法技术解读库已保留"
        elif interp.get("mode") == "interpret_config_disabled":
            interp_tail = ""
        elif interp.get("total_candidates") is not None:
            interp_tail = (
                f"；方法技术解读 本轮 {interp.get('written', 0)} 条，失败 {interp.get('failed', 0)}"
                f"（候选 {interp.get('total_candidates', 0)}，此前已有 {interp.get('already_done_before', 0)}）"
            )
        elif "total" in interp:
            interp_tail = f"；方法技术解读 {interp.get('written', 0)} 条，失败 {interp.get('failed', 0)}"
        else:
            interp_tail = ""
        if include_business_interpretation:
            if biz.get("skipped"):
                if biz.get("mode") == "business_config_disabled":
                    interp_tail += "；业务解读未执行（请启用 business_interpretation 与 vectordb-business）"
            elif biz.get("candidates_class") is not None:
                if (biz.get("total_targets") or 0) == 0:
                    interp_tail += "；业务解读：本轮无新增（待解读项已在库中或候选为空）"
                else:
                    interp_tail += (
                        f"；业务解读 本轮成功 {biz.get('written', 0)}，失败 {biz.get('failed', 0)}"
                        f"（类 {biz.get('todo_this_run_class', 0)}/候选 {biz.get('candidates_class', 0)}，"
                        f"API {biz.get('todo_this_run_api', 0)}/候选 {biz.get('candidates_api', 0)}，"
                        f"模块 {biz.get('todo_this_run_module', 0)}/候选 {biz.get('candidates_module', 0)}）"
                    )
        self._emit(
            "meta",
            _success_msg=(
                f"完成：{result.get('stage', '?')}，"
                f"节点 {result.get('graph_nodes', 0)}，边 {result.get('graph_edges', 0)}"
                + (f" — {_msg}" if _msg else "")
                + interp_tail
            ),
        )

    def finalize_file_not_found(self, e: FileNotFoundError) -> None:
        self._emit("status", status=f"错误：{e}")
        self._emit("meta", _success_msg=None)

    def finalize_error(self, e: BaseException) -> None:
        self._emit("status", status=f"流水线异常：{e!r}")
        self._emit("meta", _success_msg=None)

    def finalize_always(self, session_pop_key: str) -> None:
        self._emit("flag", running=False, completed=True)
        st.session_state.pop(session_pop_key, None)


class InterpretOnlyUiSubscription:
    """仅解读模式：进度条、步骤、统计与 checklist。"""

    def __init__(
        self,
        *,
        coordinator: PipelineLiveCoordinator,
        pl: dict[str, Any],
        interp_svc: InterpretationProgressRepository,
        sf_path_resolved: str,
        config_path: Path,
        interpret_skip_steps: Callable[[], list[str]],
    ) -> None:
        self._coord = coordinator
        self._pl = pl
        self._interp_svc = interp_svc
        self._sf_path_resolved = sf_path_resolved
        self._config_path = config_path
        self._interpret_skip_steps = interpret_skip_steps
        self._interp_stats_io: dict[str, tuple[int, int]] = {}
        self._used_persisted_io: set[str] = set()
        self._last_progress_persist_at = 0.0
        self._progress_persist_interval_sec = 2.0

    def _emit(self, event_type: str, **payload: Any) -> None:
        self._coord.emit(event_type, **payload)  # type: ignore[arg-type]

    def on_thread_started(self) -> None:
        self._emit("status", status="解读运行中…")
        self._emit("progress", progress_md="开始  \n`[░░░░░░░░░░░░░░░░░░░░] 0%`")
        self._emit("steps", set_steps=self._interpret_skip_steps())

        # 预填充双阶段进度：当文件已记录技术/业务均已完成时，
        # 避免 UI 先只出现技术条，后续再出现业务条（尤其是“增量续跑全已完成”场景）。
        try:
            persisted = self._interp_svc.load(self._sf_path_resolved) or {}
            for phase in (InterpretPhase.TECH.value, InterpretPhase.BIZ.value):
                pv = (persisted.get(phase, {}) or {})
                done = int(pv.get("done", 0) or 0)
                total = int(pv.get("total", 0) or 0)
                if total > 0:
                    self._interp_stats_io[phase] = (done, total)

            if self._interp_stats_io:
                lines: list[str] = []
                if InterpretPhase.TECH.value in self._interp_stats_io:
                    d, t = self._interp_stats_io[InterpretPhase.TECH.value]
                    lines.append(f"技术解读进度：**{d}/{t}**")
                if InterpretPhase.BIZ.value in self._interp_stats_io:
                    d, t = self._interp_stats_io[InterpretPhase.BIZ.value]
                    lines.append(f"业务解读进度：**{d}/{t}**")
                self._emit("meta", stats_md="  \n".join(lines) if lines else "")
                self._emit("meta", interp_stats=dict(self._interp_stats_io))
        except Exception:
            pass

    def on_progress(self, cur: int, tot: int, m: str) -> None:
        if _INTERP_TECH in self._interp_stats_io:
            d, t = self._interp_stats_io[_INTERP_TECH]
            frac = min(d / t, 1.0) if t else 0.0
            label = f"技术解读进度：{d}/{t}"
        elif _INTERP_BIZ in self._interp_stats_io:
            d, t = self._interp_stats_io[_INTERP_BIZ]
            frac = min(d / t, 1.0) if t else 0.0
            label = f"业务解读进度：{d}/{t}"
        else:
            frac = min(cur / tot, 1.0) if tot else 0.0
            label = m or "进度"
        self._emit("progress", progress_frac=frac, progress_label=label)
        _seg = DISPLAY_THEME.text_progress_bar_segments
        b = int(round(frac * _seg))
        self._emit(
            "progress",
            progress_md=f"{label}  \n`[{'█' * b}{'░' * (_seg - b)}] {int(frac * 100)}%`",
        )

        now = time.monotonic()
        if now - self._last_progress_persist_at >= self._progress_persist_interval_sec:
            self._last_progress_persist_at = now
            try:
                tech_done, tech_total = self._interp_stats_io.get(_INTERP_TECH, (0, 0))
                biz_done, biz_total = self._interp_stats_io.get(_INTERP_BIZ, (0, 0))
                self._interp_svc.save(
                    self._sf_path_resolved,
                    tech_done,
                    tech_total,
                    biz_done,
                    biz_total,
                )
            except Exception:
                pass

    def on_step(self, x: str) -> None:
        pl = self._pl
        steps = list(pl.get("steps", []) or self._interpret_skip_steps())
        if x not in steps:
            steps.append(x)
            self._emit("steps", set_steps=steps)
        if "请求后端" in x and "实际使用" in x:
            self._emit(
                "meta",
                llm_backend_info=x.replace("技术解读：", "").replace("业务解读：", "").strip(),
            )

    def on_interpretation_stats(self, done: int, total: int, phase: InterpretPhase) -> None:
        pk = phase.value
        _file = self._interp_svc.load(self._sf_path_resolved) or {}
        _file_phase = (_file.get(pk, {}) or {})
        file_done = int(_file_phase.get("done", 0) or 0)
        file_total = int(_file_phase.get("total", 0) or 0)

        _, _src = self._interp_svc.get(self._sf_path_resolved, str(self._config_path))

        chosen_done = max(int(done or 0), file_done)
        chosen_total = int(file_total) if file_total > 0 else int(total or 0)
        if chosen_total > 0:
            chosen_done = min(chosen_done, chosen_total)

        if chosen_done > int(done or 0) and file_done > 0:
            self._used_persisted_io.add(pk)
            self._emit("meta", progress_source=_src)
        else:
            self._used_persisted_io.discard(pk)
            self._emit("meta", progress_source=None)

        self._interp_stats_io[pk] = (chosen_done, chosen_total)
        lines = []
        if _INTERP_TECH in self._interp_stats_io:
            d, t = self._interp_stats_io[_INTERP_TECH]
            s = "（来自上次运行记录）" if _INTERP_TECH in self._used_persisted_io else ""
            lines.append(f"技术解读进度：**{d}/{t}**{s}")
        if _INTERP_BIZ in self._interp_stats_io:
            d, t = self._interp_stats_io[_INTERP_BIZ]
            s = "（来自上次运行记录）" if _INTERP_BIZ in self._used_persisted_io else ""
            lines.append(f"业务解读进度：**{d}/{t}**{s}")
        self._emit("meta", stats_md="  \n".join(lines) if lines else "")
        self._emit("meta", interp_stats=dict(self._interp_stats_io))
        if _INTERP_TECH in self._interp_stats_io:
            _d, _t = self._interp_stats_io[_INTERP_TECH]
            self._emit(
                "progress",
                progress_frac=min(_d / _t, 1.0) if _t else 0.0,
                progress_label=f"技术解读进度：{_d}/{_t}",
            )
            _steps = list(self._pl.get("steps") or [])
            if _steps and "技术解读：候选方法" in str(_steps[-1]):
                _m = re.search(r"LLM:\s*(\w+)", str(_steps[-1]))
                _backend = _m.group(1) if _m else "ollama"
                _steps[-1] = (
                    f"技术解读：候选方法 {_t} 条，其中已存在解读 {_d} 条，"
                    f"本轮计划新解读 {_t - _d} 条（LLM: {_backend}）"
                )
                self._emit("steps", set_steps=_steps)
        elif _INTERP_BIZ in self._interp_stats_io:
            _d, _t = self._interp_stats_io[_INTERP_BIZ]
            self._emit(
                "progress",
                progress_frac=min(_d / _t, 1.0) if _t else 0.0,
                progress_label=f"业务解读进度：{_d}/{_t}",
            )
        _td, _tt = self._interp_stats_io.get(_INTERP_TECH, (0, 0))
        _bd, _bt = self._interp_stats_io.get(_INTERP_BIZ, (0, 0))
        try:
            self._interp_svc.save(self._sf_path_resolved, _td, _tt, _bd, _bt)
        except Exception:
            pass

    def on_item_list_tech(self, items: Any) -> None:
        if items and isinstance(items[0], (list, tuple)):
            self._emit(
                "meta",
                checklist_tech=[(s, bool(d)) for s, d in items],
                interpret_current_tech=None,
            )
        else:
            self._emit("meta", checklist_tech=[(s, False) for s in items], interpret_current_tech=None)

    def on_item_list_biz(self, items: list[str]) -> None:
        self._emit("meta", checklist_biz=[(s, False) for s in items], interpret_current_biz=None)

    def on_item_started_tech(self, label: str, _phase: InterpretPhase = InterpretPhase.TECH) -> None:
        self._emit("meta", interpret_current_tech=label)

    def on_item_started_biz(self, label: str, _phase: InterpretPhase = InterpretPhase.BIZ) -> None:
        self._emit("meta", interpret_current_biz=label)

    def on_item_completed_tech(self, sig: str, done: bool) -> None:
        pl = self._pl
        for i, (s, _) in enumerate(pl.get("checklist_tech", [])):
            if s == sig:
                lst = list(pl["checklist_tech"])
                lst[i] = (s, done)
                self._emit("meta", checklist_tech=lst, interpret_current_tech=None)
                break
        if done and _INTERP_TECH in self._interp_stats_io:
            d, t = self._interp_stats_io[_INTERP_TECH]
            self._interp_stats_io[_INTERP_TECH] = (d + 1, t)
            nd, nt = d + 1, t
            lines = [f"技术解读进度：**{nd}/{nt}**"]
            if _INTERP_BIZ in self._interp_stats_io:
                lines.append(
                    f"业务解读进度：**{self._interp_stats_io[_INTERP_BIZ][0]}/{self._interp_stats_io[_INTERP_BIZ][1]}**"
                )
            self._emit("meta", stats_md="  \n".join(lines))
            self._emit("meta", interp_stats=dict(self._interp_stats_io))
            self._emit(
                "progress",
                progress_frac=min(nd / nt, 1.0) if nt else 0.0,
                progress_label=f"技术解读进度：{nd}/{nt}",
            )
            _steps = list(pl.get("steps") or [])
            if _steps and "技术解读：候选方法" in str(_steps[-1]):
                _m = re.search(r"LLM:\s*(\w+)", str(_steps[-1]))
                _backend = _m.group(1) if _m else "ollama"
                _steps[-1] = (
                    f"技术解读：候选方法 {nt} 条，其中已存在解读 {nd} 条，"
                    f"本轮计划新解读 {nt - nd} 条（LLM: {_backend}）"
                )
                self._emit("steps", set_steps=_steps)
            _td, _tt = self._interp_stats_io[_INTERP_TECH]
            _bd, _bt = self._interp_stats_io.get(_INTERP_BIZ, (0, 0))
            try:
                self._interp_svc.save(self._sf_path_resolved, _td, _tt, _bd, _bt)
            except Exception:
                pass

    def on_item_completed_biz(self, sig: str, done: bool) -> None:
        pl = self._pl
        for i, (s, _) in enumerate(pl.get("checklist_biz", [])):
            if s == sig:
                lst = list(pl["checklist_biz"])
                lst[i] = (s, done)
                self._emit("meta", checklist_biz=lst, interpret_current_biz=None)
                break
        if done and _INTERP_BIZ in self._interp_stats_io:
            d, t = self._interp_stats_io[_INTERP_BIZ]
            self._interp_stats_io[_INTERP_BIZ] = (d + 1, t)
            nd, nt = d + 1, t
            lines = []
            if _INTERP_TECH in self._interp_stats_io:
                lines.append(
                    f"技术解读进度：**{self._interp_stats_io[_INTERP_TECH][0]}/{self._interp_stats_io[_INTERP_TECH][1]}**"
                )
            lines.append(f"业务解读进度：**{nd}/{nt}**")
            self._emit("meta", stats_md="  \n".join(lines))
            self._emit("meta", interp_stats=dict(self._interp_stats_io))
            self._emit(
                "progress",
                progress_frac=min(nd / nt, 1.0) if nt else 0.0,
                progress_label=f"业务解读进度：{nd}/{nt}",
            )
            _td, _tt = self._interp_stats_io.get(_INTERP_TECH, (0, 0))
            _bd, _bt = self._interp_stats_io[_INTERP_BIZ]
            try:
                self._interp_svc.save(self._sf_path_resolved, _td, _tt, _bd, _bt)
            except Exception:
                pass

    def finalize_success(self, result: dict[str, Any]) -> None:
        _msg = result.get("message", "完成")
        ir = result.get("interpretation") or {}
        br = result.get("business_interpretation") or {}
        if ir.get("skipped") and br.get("skipped"):
            _reasons = (
                [ir.get("reason", ""), br.get("reason", "")]
                if ir.get("reason") or br.get("reason")
                else []
            )
            if _reasons:
                _msg = f"未执行解读：{'；'.join(r for r in _reasons if r)}"
        self._emit("status", status=_msg)
        _seg = DISPLAY_THEME.text_progress_bar_segments
        self._emit("progress", progress_md="进度  \n`[" + "█" * _seg + "] 100%`")
        self._emit("progress", progress_frac=1.0, progress_label="完成")
        io = self._interp_stats_io
        self._emit("meta", _interp_stats=dict(io), _sf_path=self._sf_path_resolved)

    def finalize_error(self, e: BaseException) -> None:
        self._emit("error", status=f"解读异常：{e!r}", traceback=traceback.format_exc())

    def finalize_always(self) -> None:
        self._emit("flag", running=False, completed=True)
