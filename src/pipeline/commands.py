from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple, Type

from src.core.context import AppContext
from src.core.domain_enums import InterpretPhase
from src.persistence.repositories import SnapshotRepository, StructureFactsRepository
from src.pipeline.run import run_interpretations_only, run_pipeline

_LOG = logging.getLogger(__name__)

# 仅对这些异常做重试（网络/IO/超时等）；其余异常立即向上抛出，避免掩盖配置/逻辑错误。
_TRANSIENT_PIPELINE_EXCEPTIONS: Tuple[Type[BaseException], ...] = (
    OSError,
    ConnectionError,
    BrokenPipeError,
    TimeoutError,
)
try:
    import httpx

    _TRANSIENT_PIPELINE_EXCEPTIONS = _TRANSIENT_PIPELINE_EXCEPTIONS + (
        httpx.ConnectError,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.RemoteProtocolError,
        httpx.TransportError,
    )
except ImportError:
    pass


@dataclass(frozen=True)
class RetryPolicy:
    """简单 retry 策略（用于临时网络/Weaviate 抖动）。

    仅对模块级 ``_TRANSIENT_PIPELINE_EXCEPTIONS`` 中的异常重试；其它异常立即抛出。
    ``KeyboardInterrupt`` / ``SystemExit`` 不会被捕获。
    """

    max_attempts: int = 1
    delay_seconds: float = 0.0


class PipelineCommand:
    """命令对象：封装流水线执行参数，便于外层统一执行/重试/入队。"""

    def execute(self) -> dict[str, Any]:
        raise NotImplementedError

    def execute_with_retry(self, retry_policy: RetryPolicy, *, on_retry: Optional[Any] = None) -> dict[str, Any]:
        last_exc: Optional[BaseException] = None
        for attempt in range(1, retry_policy.max_attempts + 1):
            try:
                return self.execute()
            except _TRANSIENT_PIPELINE_EXCEPTIONS as e:
                last_exc = e
                if attempt >= retry_policy.max_attempts:
                    _LOG.error(
                        "流水线执行已达最大重试次数 %s，最后错误: %s: %s",
                        retry_policy.max_attempts,
                        type(e).__name__,
                        e,
                        exc_info=True,
                    )
                    raise
                _LOG.warning(
                    "流水线执行第 %s/%s 次失败（可重试）: %s: %s",
                    attempt,
                    retry_policy.max_attempts,
                    type(e).__name__,
                    e,
                    exc_info=True,
                )
                if on_retry:
                    try:
                        on_retry(attempt, e)
                    except Exception:
                        _LOG.debug("on_retry 回调异常（已忽略）", exc_info=True)
                if retry_policy.delay_seconds > 0:
                    time.sleep(retry_policy.delay_seconds)
        raise last_exc  # pragma: no cover


class FullPipelineCommand(PipelineCommand):
    def __init__(
        self,
        *,
        config_path: str,
        include_method_interpretation: Optional[bool],
        include_business_interpretation: Optional[bool],
        progress_callback: Optional[Any],
        step_callback: Optional[Any],
        item_list_callback: Optional[Any],
        item_completed_callback: Optional[Any],
        item_started_callback: Optional[Callable[[str, InterpretPhase], None]],
        interpretation_stats_callback: Optional[Callable[[int, int, InterpretPhase], None]],
        structure_facts_repo: Optional[StructureFactsRepository],
        snapshot_repo: Optional[SnapshotRepository],
        app_context: Optional[AppContext] = None,
    ):
        self._config_path = config_path
        self._include_method_interpretation = include_method_interpretation
        self._include_business_interpretation = include_business_interpretation
        self._progress_callback = progress_callback
        self._step_callback = step_callback
        self._item_list_callback = item_list_callback
        self._item_completed_callback = item_completed_callback
        self._item_started_callback = item_started_callback
        self._interpretation_stats_callback = interpretation_stats_callback
        self._structure_facts_repo = structure_facts_repo
        self._snapshot_repo = snapshot_repo
        self._app_context = app_context

    def execute(self) -> dict[str, Any]:
        return run_pipeline(
            config_path=self._config_path,
            progress_callback=self._progress_callback,
            step_callback=self._step_callback,
            include_method_interpretation=self._include_method_interpretation,
            include_business_interpretation=self._include_business_interpretation,
            item_list_callback=self._item_list_callback,
            item_completed_callback=self._item_completed_callback,
            item_started_callback=self._item_started_callback,
            interpretation_stats_callback=self._interpretation_stats_callback,
            structure_facts_repo=self._structure_facts_repo,
            snapshot_repo=self._snapshot_repo,
            app_context=self._app_context,
        )


class InterpretOnlyCommand(PipelineCommand):
    def __init__(
        self,
        *,
        config_path: str,
        structure_facts_json: str,
        progress_callback: Optional[Any],
        step_callback: Optional[Any],
        include_method_interpretation: bool,
        include_business_interpretation: bool,
        item_list_callback_tech: Optional[Any],
        item_list_callback_biz: Optional[Any],
        item_completed_callback_tech: Optional[Any],
        item_completed_callback_biz: Optional[Any],
        item_started_callback_tech: Optional[Callable[[str, InterpretPhase], None]],
        item_started_callback_biz: Optional[Callable[[str, InterpretPhase], None]],
        interpretation_stats_callback: Optional[Callable[[int, int, InterpretPhase], None]],
        structure_facts_repo: Optional[StructureFactsRepository],
        app_context: Optional[AppContext] = None,
    ):
        self._config_path = config_path
        self._sf_json = structure_facts_json
        self._progress_callback = progress_callback
        self._step_callback = step_callback
        self._include_method_interpretation = include_method_interpretation
        self._include_business_interpretation = include_business_interpretation
        self._item_list_callback_tech = item_list_callback_tech
        self._item_list_callback_biz = item_list_callback_biz
        self._item_completed_callback_tech = item_completed_callback_tech
        self._item_completed_callback_biz = item_completed_callback_biz
        self._item_started_callback_tech = item_started_callback_tech
        self._item_started_callback_biz = item_started_callback_biz
        self._interpretation_stats_callback = interpretation_stats_callback
        self._structure_facts_repo = structure_facts_repo
        self._app_context = app_context

    def execute(self) -> dict[str, Any]:
        return run_interpretations_only(
            config_path=self._config_path,
            structure_facts_json=self._sf_json,
            progress_callback=self._progress_callback,
            step_callback=self._step_callback,
            include_method_interpretation=self._include_method_interpretation,
            include_business_interpretation=self._include_business_interpretation,
            item_list_callback_tech=self._item_list_callback_tech,
            item_list_callback_biz=self._item_list_callback_biz,
            item_completed_callback_tech=self._item_completed_callback_tech,
            item_completed_callback_biz=self._item_completed_callback_biz,
            item_started_callback_tech=self._item_started_callback_tech,
            item_started_callback_biz=self._item_started_callback_biz,
            interpretation_stats_callback=self._interpretation_stats_callback,
            structure_facts_repo=self._structure_facts_repo,
            app_context=self._app_context,
        )

