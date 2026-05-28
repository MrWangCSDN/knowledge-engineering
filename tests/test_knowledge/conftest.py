"""tests/test_knowledge/ 共享 fixtures。

设计目的：当 tests/test_auth 先跑后，某些 test 调用 logging.config.dictConfig 时
`disable_existing_loggers=True` 会把已注册的 logger 全部 disable（含本目录待测的
src.knowledge.composite_knowledge_store logger）。结果：caplog 抓不到日志，
本目录 4 个 caplog 验证测试静默 fail。

修复：autouse fixture 在每个本目录测试前强制 reset logger.disabled = False。
"""
# logging：标准库，用于操作 logger 状态
import logging

# pytest：测试框架（autouse fixture）
import pytest


@pytest.fixture(autouse=True)
def _reenable_composite_logger():
    """每个 test_knowledge 测试前强制 re-enable 本模块 logger。

    autouse=True：自动注入到本目录每个测试，不用显式声明依赖。
    yield 前的代码在测试**开始前**跑；yield 后的代码在测试**结束后**跑（teardown）。

    我们只需 setup（re-enable），不需要 teardown（保持 enable 状态本身无害）。
    """
    # 拿到 composite_knowledge_store 模块的 logger（名字 = 模块全限定名）
    log = logging.getLogger("src.knowledge.composite_knowledge_store")
    # 强制 disabled=False；之前 dictConfig 可能设过 True
    log.disabled = False
    # `yield` 把控制权交给测试体；没值意味着测试体不需要 fixture 返回值
    yield
