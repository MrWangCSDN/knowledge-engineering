"""流水线运行服务：全量构建与仅运行解读的线程封装。"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

from src.app.services.pipeline_live_coordinator import PipelineLiveCoordinator
from src.app.services.pipeline_runner_ui_subscribers import (
    FullPipelineUiSubscription,
    InterpretOnlyUiSubscription,
)
from src.app.ui.streamlit_keys import SessionKeys
from src.pipeline.commands import FullPipelineCommand, InterpretOnlyCommand, RetryPolicy
from src.persistence.repositories import (
    InterpretationProgressRepository,
    SnapshotRepository,
    StructureFactsRepository,
)

try:
    from streamlit.runtime.scriptrunner import add_script_run_ctx
except ImportError:
    add_script_run_ctx = None


class PipelineRunner:
    """封装全量流水线与仅解读模式的线程执行。

    UI 状态归约见 `PipelineLiveCoordinator` / `apply_pipeline_live_event`；
    回调与收尾见 `pipeline_runner_ui_subscribers`。
    """

    @staticmethod
    def interpret_skip_steps() -> list[str]:
        from src.app.i18n.ui_strings import interpret_skip_steps as _steps

        return _steps()

    def __init__(
        self,
        interp_progress_repo: InterpretationProgressRepository,
        structure_facts_repo: StructureFactsRepository,
        snapshot_repo: SnapshotRepository,
        get_pipeline_live: Callable[[], dict],
        get_graph_optional: Callable[[], Any],
        root: Path,
    ):
        self._interp_svc = interp_progress_repo
        self._structure_repo = structure_facts_repo
        self._snapshot_repo = snapshot_repo
        self._get_pipeline_live = get_pipeline_live
        self._get_graph = get_graph_optional
        self._root = root

    def run_full_pipeline(
        self,
        config_path: Path,
        include_method_interpretation: bool,
        include_business_interpretation: bool,
        sf_path_pipeline: str,
        session_pop_key: str = SessionKeys.FULL_PIPELINE_RUNNING,
    ) -> None:
        """启动全量流水线线程。调用前需已设置 pipeline_live 初始状态。"""
        pl = self._get_pipeline_live()
        coord = PipelineLiveCoordinator(pl)
        sub = FullPipelineUiSubscription(
            coordinator=coord,
            pl=pl,
            interp_svc=self._interp_svc,
            sf_path_pipeline=sf_path_pipeline,
            get_graph=self._get_graph,
            snapshot_repo=self._snapshot_repo,
            root=self._root,
        )

        def _run() -> None:
            cfg: dict = {}
            try:
                try:
                    from src.pipeline.gateways import load_project_config

                    proj = load_project_config(str(config_path))
                    cfg = proj.model_dump()
                except Exception:
                    cfg = {}

                cmd = FullPipelineCommand(
                    config_path=str(config_path),
                    include_method_interpretation=include_method_interpretation,
                    include_business_interpretation=include_business_interpretation,
                    progress_callback=sub.on_pipeline_progress,
                    step_callback=sub.on_step,
                    item_list_callback=sub.on_item_list,
                    item_completed_callback=sub.on_item_completed,
                    item_started_callback=sub.on_item_started,
                    interpretation_stats_callback=sub.on_interpretation_stats,
                    structure_facts_repo=self._structure_repo,
                    snapshot_repo=self._snapshot_repo,
                )
                result = cmd.execute_with_retry(RetryPolicy(max_attempts=1))
                sub.finalize_success(
                    result=result,
                    cfg=cfg,
                    include_method_interpretation=include_method_interpretation,
                    include_business_interpretation=include_business_interpretation,
                )
            except FileNotFoundError as e:
                sub.finalize_file_not_found(e)
            except Exception as e:
                sub.finalize_error(e)
            finally:
                sub.finalize_always(session_pop_key)

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    def run_interpret_only(
        self,
        config_path: Path,
        sf_path: Path,
        include_tech: bool,
        include_biz: bool,
        sf_path_resolved: str,
        session_pop_key: str = SessionKeys.INTERPRET_PIPELINE_RUNNING,
    ) -> None:
        """启动仅解读线程。与 run_full_pipeline 一致：直接修改 pl，并用 add_script_run_ctx 附加上下文。"""
        pl = self._get_pipeline_live()
        coord = PipelineLiveCoordinator(pl)
        sub = InterpretOnlyUiSubscription(
            coordinator=coord,
            pl=pl,
            interp_svc=self._interp_svc,
            sf_path_resolved=sf_path_resolved,
            config_path=config_path,
            interpret_skip_steps=self.interpret_skip_steps,
        )

        def _run() -> None:
            try:
                sub.on_thread_started()
                cmd = InterpretOnlyCommand(
                    config_path=str(config_path),
                    structure_facts_json=sf_path,
                    progress_callback=sub.on_progress,
                    step_callback=sub.on_step,
                    include_method_interpretation=include_tech,
                    include_business_interpretation=include_biz,
                    item_list_callback_tech=sub.on_item_list_tech,
                    item_list_callback_biz=sub.on_item_list_biz,
                    item_completed_callback_tech=sub.on_item_completed_tech,
                    item_completed_callback_biz=sub.on_item_completed_biz,
                    item_started_callback_tech=sub.on_item_started_tech,
                    item_started_callback_biz=sub.on_item_started_biz,
                    interpretation_stats_callback=sub.on_interpretation_stats,
                    structure_facts_repo=self._structure_repo,
                )
                _res = cmd.execute_with_retry(RetryPolicy(max_attempts=1))
                sub.finalize_success(_res)
            except Exception as e:
                sub.finalize_error(e)
            finally:
                sub.finalize_always()

        t = threading.Thread(target=_run, daemon=True)
        if add_script_run_ctx:
            add_script_run_ctx(t)
        t.start()
