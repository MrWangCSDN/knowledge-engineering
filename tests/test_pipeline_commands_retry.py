"""PipelineCommand.execute_with_retry：仅对瞬时异常重试，并记录日志。"""
from __future__ import annotations

import logging

import pytest

from src.pipeline.commands import PipelineCommand, RetryPolicy


class _TransientThenOkCommand(PipelineCommand):
    def __init__(self) -> None:
        self.calls = 0

    def execute(self) -> dict:
        self.calls += 1
        if self.calls < 2:
            raise ConnectionError("simulated transient")
        return {"ok": True}


class _AlwaysValueErrorCommand(PipelineCommand):
    def execute(self) -> dict:
        raise ValueError("non-transient")


def test_retry_recovers_from_connection_error() -> None:
    cmd = _TransientThenOkCommand()
    out = cmd.execute_with_retry(RetryPolicy(max_attempts=3, delay_seconds=0))
    assert out == {"ok": True}
    assert cmd.calls == 2


def test_retry_does_not_swallow_value_error() -> None:
    cmd = _AlwaysValueErrorCommand()
    with pytest.raises(ValueError, match="non-transient"):
        cmd.execute_with_retry(RetryPolicy(max_attempts=3, delay_seconds=0))


def test_retry_logs_warning_on_transient(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    cmd = _TransientThenOkCommand()
    cmd.execute_with_retry(RetryPolicy(max_attempts=3, delay_seconds=0))
    assert any("可重试" in r.message for r in caplog.records)
