"""TokenBatcher：把多个小 token 攒成批，降低 SSE 事件密度。

设计动机（[[首页设计]] §18 v1.7）：
  - LLM 流式时每个 chunk 可能就 1-3 个汉字 / 1 个 token
  - 每个 chunk emit 一个 SSE 事件 → 千字回答可能 500+ 事件
  - 前端 / 网络 / 浏览器都吃不消
  - 攒批：达到 N 字符 OR M 毫秒就 flush 成一条 SSE

策略：
  - 调用 `await batcher.add(chunk)` → 返回 Optional[str]（攒够了就给完整批；否则 None）
  - 流末调用 `await batcher.flush()` → 返回剩余 buffer
  - 时间维度：到达 max_ms 时下一个 add 会触发 flush
"""
import asyncio

import pytest

# 待实现的类
from src.service.qa_engine.token_batcher import TokenBatcher


@pytest.mark.asyncio
async def test_batcher_returns_none_until_min_chars_reached() -> None:
    """累计 chars < min_chars 时，add 返回 None；达到阈值返回攒批字符串。"""
    b = TokenBatcher(min_chars=10, max_ms=10000)  # 时间设大，让字符阈值生效
    # 累计到 9 个字符都不应触发 flush
    assert await b.add("12345") is None
    assert await b.add("6789") is None
    # 第 10 个字符触发 flush
    out = await b.add("0")
    assert out == "1234567890"
    # flush 后 buffer 清空：下次 add 重新攒
    assert await b.add("xxx") is None


@pytest.mark.asyncio
async def test_batcher_flushes_on_explicit_flush_call() -> None:
    """流末显式 flush() 返回所有剩余 buffer，即便没达到阈值。"""
    b = TokenBatcher(min_chars=100, max_ms=10000)
    await b.add("a")
    await b.add("bc")
    # flush() 应该返回 'abc' 并清空
    assert await b.flush() == "abc"
    # 二次 flush 返回 None（buffer 已空）
    assert await b.flush() is None


@pytest.mark.asyncio
async def test_batcher_flushes_when_max_ms_elapsed() -> None:
    """时间维度：累计字符没到阈值，但距离上次 flush 超过 max_ms 就 flush。

    用很小的 max_ms（50ms）+ 真实 asyncio.sleep 测试；
    第一个 add 立刻进入 buffer，等 80ms 后下一个 add 触发时间 flush。
    """
    b = TokenBatcher(min_chars=1000, max_ms=50)  # 字符阈值故意调大
    assert await b.add("a") is None  # 没到字符也没超时
    # 等过 max_ms
    await asyncio.sleep(0.08)
    # 这一个 add 应该触发时间 flush，返回包含之前累积 + 当前的内容
    out = await b.add("b")
    assert out == "ab"
