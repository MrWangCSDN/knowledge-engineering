"""解读 Runner 基类：统一回调与异常安全辅助。"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from src.core.domain_enums import InterpretPhase

_LOG = logging.getLogger(__name__)


@dataclass
class BaseInterpretationRunner:
    """提供通用的 step/progress/item 回调封装。

    item_started_callback(label, phase)：phase 为 InterpretPhase，用于 UI 标记「正在解读」。
    """

    step_callback: Optional[Callable[[str], None]] = None
    progress_callback: Optional[Callable[[int, int, str], None]] = None
    item_completed_callback: Optional[Callable[[str, bool], None]] = None
    item_started_callback: Optional[Callable[[str, InterpretPhase], None]] = None
    item_list_callback: Optional[Callable[[Any], None]] = None

    def step(self, msg: str) -> None:
        if self.step_callback:
            try:
                self.step_callback(msg)
            except Exception:
                _LOG.debug("step_callback 失败（已忽略）", exc_info=True)

    def progress(self, current: int, total: int, message: str) -> None:
        if self.progress_callback:
            try:
                self.progress_callback(current, total, message)
            except Exception:
                _LOG.debug("progress_callback 失败（已忽略）", exc_info=True)

    def complete_item(self, label: str, done: bool) -> None:
        if self.item_completed_callback:
            try:
                self.item_completed_callback(label, done)
            except Exception:
                _LOG.debug("item_completed_callback 失败（已忽略）", exc_info=True)

    def start_item(self, label: str, phase: InterpretPhase = InterpretPhase.TECH) -> None:
        if self.item_started_callback:
            try:
                self.item_started_callback(label, phase)
            except Exception:
                _LOG.debug("item_started_callback 失败（已忽略）", exc_info=True)

    def publish_item_list(self, items: Any) -> None:
        if self.item_list_callback:
            try:
                self.item_list_callback(items)
            except Exception:
                _LOG.debug("item_list_callback 失败（已忽略）", exc_info=True)

