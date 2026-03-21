"""流水线 UI 镜像状态：领域线程内 emit 事件，由协调器归约到 ``pipeline_live`` dict。

与 ``PipelineRunner`` 解耦：Runner 负责起线程与组装回调；本模块负责「事件 → pl 状态」的单一职责。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

PipelineLiveEventType = Literal["status", "progress", "steps", "flag", "meta", "error"]


@dataclass(frozen=True)
class PipelineLiveEvent:
    """单条 UI 更新事件（观察者/Mediator 中的事件载荷）。"""

    type: PipelineLiveEventType
    payload: dict[str, Any]


def apply_pipeline_live_event(pl: dict[str, Any], event: PipelineLiveEvent) -> None:
    """将事件归约到 ``pl``（就地修改）。供单测与自定义 UI 订阅复用。"""
    t = event.type
    p = event.payload
    if t == "status":
        pl["status"] = p.get("status", pl.get("status"))
        return
    if t == "progress":
        if "progress_frac" in p:
            pl["progress_frac"] = p["progress_frac"]
        if "progress_label" in p:
            pl["progress_label"] = p["progress_label"]
        if "progress_md" in p:
            pl["progress_md"] = p["progress_md"]
        return
    if t == "steps":
        if "append_step" in p:
            steps = list(pl.get("steps", []))
            steps.append(p["append_step"])
            pl["steps"] = steps
        elif "set_steps" in p:
            pl["steps"] = list(p["set_steps"])
        return
    if t == "flag":
        for k, v in p.items():
            pl[k] = v
        return
    if t == "meta":
        for k, v in p.items():
            pl[k] = v
        return
    if t == "error":
        pl["_error_tb"] = p.get("traceback", "")
        pl["status"] = p.get("status", pl.get("status"))


class PipelineLiveCoordinator:
    """附着在某个 ``pipeline_live`` dict 上，提供 ``emit`` 入口。"""

    __slots__ = ("_pl",)

    def __init__(self, pl: dict[str, Any]) -> None:
        self._pl = pl

    def emit(self, event_type: PipelineLiveEventType, **payload: Any) -> None:
        apply_pipeline_live_event(self._pl, PipelineLiveEvent(type=event_type, payload=payload))
