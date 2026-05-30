"""PipelineCommand.execute_with_retry：仅对瞬时异常重试，并记录日志。"""
from __future__ import annotations

import logging

import pytest

from src.pipeline.commands import PipelineCommand, RetryPolicy


@pytest.fixture(autouse=True)
def _reenable_commands_logger():
    """每个测试前强制 re-enable src.pipeline.commands 的 logger。

    背景：当 tests/test_auth 等更早 collect 的测试先跑后，某些 test 触发
    logging.config.dictConfig（disable_existing_loggers=True 是 dictConfig 默认值）
    会把当时已注册的 logger 全部置 disabled=True，含本文件被测的 src.pipeline.commands。
    后果：caplog 抓不到 WARNING，test_retry_logs_warning_on_transient 静默 fail（单独跑正常）。
    这是既有的测试隔离问题，沿用 tests/test_knowledge/conftest.py 的同款修复手法。

    autouse=True：自动作用于本文件每个测试，无需在测试签名里显式声明；
    yield 之前是 setup（测试开始前跑），yield 之后是 teardown（此处不需要）。
    """
    # logging.getLogger(name)：按名取 logger（不存在则新建）；.disabled=False 解除"被禁用"，让记录正常发出
    logging.getLogger("src.pipeline.commands").disabled = False
    # yield 把控制权交回测试体；这里只需 setup，故 yield 后无 teardown 代码
    yield


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
