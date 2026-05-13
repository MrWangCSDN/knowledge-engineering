"""TokenBatcher：流式 LLM token 攒批工具。

把单字符级 chunk 攒成 N 字 / M ms 的批，减少 SSE 事件密度。

用法：
    batcher = TokenBatcher(min_chars=20, max_ms=80)
    async for chunk in llm.complete_stream(...):
        batch = await batcher.add(chunk)
        if batch is not None:
            yield format_sse("token", {"delta": batch})
    # 流末把残留也发出去
    final = await batcher.flush()
    if final:
        yield format_sse("token", {"delta": final})

设计文档：[[首页设计]] §18 v1.7
"""
from __future__ import annotations

# time.monotonic：单调递增时钟，不受系统时间调整影响，适合做"过了多久"判断
# 比 time.time() 更安全（time.time() 在 NTP 同步时可能回退）
import time
from typing import Optional


class TokenBatcher:
    """N 字符 OR M 毫秒触发 flush 的攒批器。

    无状态对外接口：
      - `await add(chunk)` → Optional[str] (有 batch 时返回完整 batch；否则 None)
      - `await flush()`    → Optional[str] (返回所有剩余 buffer)

    线程不安全：依赖单 event loop 串行调用（FastAPI / asyncio 默认场景就这样）。
    """

    def __init__(self, min_chars: int = 20, max_ms: int = 80) -> None:
        """
        :param min_chars: 攒到这么多字符就 flush（典型 20-50）
        :param max_ms:    距离上次 flush 超过这么多毫秒就 flush（典型 50-100）
        """
        # 校验：两个参数都必须正数
        if min_chars < 1:
            raise ValueError("min_chars 必须 >= 1")
        if max_ms < 1:
            raise ValueError("max_ms 必须 >= 1")
        self._min_chars = min_chars
        # 把 ms 转成 s（time.monotonic 返回 float seconds）
        self._max_seconds = max_ms / 1000.0
        # buffer：list of str（最后用 ''.join() 拼，比反复 += 高效）
        self._buffer: list[str] = []
        # buffer 累计字符数（避免每次 len 整个 list join 一遍）
        self._buffer_chars = 0
        # 上次 flush 的时间戳；初始化为现在，让"第一个 chunk 的 0ms 不会立即触发"
        self._last_flush_at = time.monotonic()

    async def add(self, chunk: str) -> Optional[str]:
        """加一个 chunk；返回 batch 字符串（如果攒够了）或 None。

        触发 flush 的条件（任一）：
          1. 加完后 buffer_chars >= min_chars
          2. 距离上次 flush 已经超过 max_seconds
        """
        if not chunk:
            return None
        self._buffer.append(chunk)
        self._buffer_chars += len(chunk)

        # 时间维度判断；放在字符之后，因为"积压了一段时间该发了"是兜底
        now = time.monotonic()
        time_due = (now - self._last_flush_at) >= self._max_seconds

        if self._buffer_chars >= self._min_chars or time_due:
            return self._do_flush()
        return None

    async def flush(self) -> Optional[str]:
        """流末调用：把所有剩余 buffer 一次性返回；buffer 已空返回 None。"""
        if not self._buffer:
            return None
        return self._do_flush()

    # ─── 内部 ─────────────────────────────────────────────────────────────

    def _do_flush(self) -> str:
        """实际 flush 逻辑：拼 buffer → 重置状态 → 返回字符串。"""
        # str.join 是 Python 拼字符串数组的标准做法（O(N) 一遍走完）
        out = "".join(self._buffer)
        self._buffer = []
        self._buffer_chars = 0
        self._last_flush_at = time.monotonic()
        return out
