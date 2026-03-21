"""业务解读分层策略：类 / API / 模块各层共用同一执行循环，避免三轮复制粘贴。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

# 标签、提示词、Weaviate 写入参数
LabelFn = Callable[[Any], str]
PromptFn = Callable[[Any], str]
AddKwargsFn = Callable[[Any, str], dict[str, Any]]


@dataclass(frozen=True)
class BusinessInterpretTierSpec:
    """单层业务解读：待处理项 + UI/进度前缀 + LLM 文本阈值 + 进度条权重 + 三个构造器。"""

    items: Sequence[Any]
    msg_prefix: str
    min_text_len: int
    pct_cap: int
    label_fn: LabelFn
    prompt_fn: PromptFn
    add_kwargs_fn: AddKwargsFn
