"""多LLM负载均衡提供者：轮询分发请求到多个后端 + 限流自动退避重试。"""
from __future__ import annotations

import itertools
import logging
import random
import threading
import time
from typing import Any

from .protocol import LLMProvider

_LOG = logging.getLogger(__name__)

# 指数退避延迟序列 (秒)
_BACKOFF_DELAYS = [30, 60, 120]


def _is_rate_limit_error(err: Exception) -> bool:
    """判断是否为限流错误 (429 / rate_limit / throttling / quota)"""
    msg = str(err).lower()
    return (
        "429" in msg
        or "rate_limit" in msg
        or "rate limit" in msg
        or "throttling" in msg
        or "quota exceeded" in msg
        or "usage limit" in msg
        or "too many requests" in msg
    )


class MultiProvider:
    """
    将多个 LLM Provider 组合为一个，请求按轮询（round-robin）分发。

    典型场景：Qwen Coding Plan + MiniMax Coding Plan，两者同时使用。

    容错策略：
    1. 单 Provider 失败 → 立即切换到下一个 Provider
    2. 所有 Provider 都 429 限流 → 指数退避 (30s/60s/120s)，最多重试 3 轮
    3. 非限流错误 → 不退避，直接抛出
    """

    def __init__(self, providers: list[LLMProvider], names: list[str] | None = None):
        if not providers:
            raise ValueError("MultiProvider: 至少需要一个 provider")
        self._providers = providers
        self._names = names or [f"provider-{i}" for i in range(len(providers))]
        self._cycle = itertools.cycle(range(len(providers)))
        self._lock = threading.Lock()
        self._call_counts = [0] * len(providers)
        self._backoff_count = 0  # 统计退避次数

    def generate(self, prompt: str, **kwargs: Any) -> str:
        """轮询分发请求，所有 Provider 限流时指数退避重试。"""
        last_error: Exception | None = None

        for attempt in range(len(_BACKOFF_DELAYS) + 1):
            with self._lock:
                start_idx = next(self._cycle)

            all_rate_limited = True
            for offset in range(len(self._providers)):
                idx = (start_idx + offset) % len(self._providers)
                provider = self._providers[idx]
                name = self._names[idx]
                try:
                    result = provider.generate(prompt, **kwargs)
                    with self._lock:
                        self._call_counts[idx] += 1
                    return result
                except Exception as e:
                    is_429 = _is_rate_limit_error(e)
                    if not is_429:
                        all_rate_limited = False
                    _LOG.warning(
                        "MultiProvider: %s 调用失败 (429=%s)，尝试下一个: %s",
                        name, is_429, str(e)[:200]
                    )
                    last_error = e
                    continue

            # 所有 Provider 都试过了
            if not all_rate_limited:
                # 不是限流，直接报错，不重试
                break

            # 全部是限流 → 退避
            if attempt < len(_BACKOFF_DELAYS):
                delay = _BACKOFF_DELAYS[attempt]
                # 加点随机抖动，避免雪崩
                jitter = random.uniform(0, 5)
                sleep_time = delay + jitter
                with self._lock:
                    self._backoff_count += 1
                _LOG.warning(
                    "MultiProvider: 所有 Provider 限流，指数退避 %.1fs (第 %d/%d 次重试)",
                    sleep_time, attempt + 1, len(_BACKOFF_DELAYS)
                )
                time.sleep(sleep_time)
            else:
                _LOG.error("MultiProvider: 退避重试 %d 次后仍然限流，放弃", len(_BACKOFF_DELAYS))

        raise RuntimeError(
            f"MultiProvider: 所有 {len(self._providers)} 个 Provider 均失败"
        ) from last_error

    def stats(self) -> dict[str, Any]:
        """返回每个 Provider 的调用次数 + 退避统计。"""
        with self._lock:
            stats = {name: count for name, count in zip(self._names, self._call_counts)}
            stats["_backoff_count"] = self._backoff_count
            return stats
