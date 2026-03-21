"""
从 ``src/app/resources/strings.{lang}.yaml`` 加载 UI 文案。

环境变量 ``KE_UI_LANG`` 可指定语言代码（默认 zh_CN）。未找到文件时回退 zh_CN。
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_RESOURCES_DIR = Path(__file__).resolve().parents[1] / "resources"
_DEFAULT_LANG = "zh_CN"


def _lang_code() -> str:
    return (os.environ.get("KE_UI_LANG") or _DEFAULT_LANG).strip() or _DEFAULT_LANG


@lru_cache(maxsize=4)
def get_ui_strings(lang: str | None = None) -> dict[str, Any]:
    """返回嵌套 dict，键与 YAML 一致。"""
    code = lang or _lang_code()
    path = _RESOURCES_DIR / f"strings.{code}.yaml"
    if not path.is_file():
        path = _RESOURCES_DIR / f"strings.{_DEFAULT_LANG}.yaml"
    if not path.is_file():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


# YAML 缺失或损坏时的兜底（与默认 strings.zh_CN 保持一致）
_FALLBACK_INTERPRET_SKIP_STEPS: tuple[str, ...] = (
    "① 结构层：解析 AST …（已跳过）",
    "② 结构层完成（已跳过）",
    "③ 语义层：术语与向量文本 …（已跳过）",
    "④ 语义层完成（已跳过）",
    "⑤ 清理 Neo4j 与 Weaviate 代码库…（已跳过）",
    "⑥ 知识层：构建内存图、写入代码向量、同步 Neo4j …（已跳过）",
    "⑦ 知识层与代码向量库已完成（已跳过）",
)


def interpret_skip_steps() -> list[str]:
    """仅解读模式工序：跳过结构/语义/知识层时的步骤列表。"""
    steps = get_ui_strings().get("pipeline", {}).get("interpret_skip_steps")
    if isinstance(steps, list) and all(isinstance(x, str) for x in steps) and steps:
        return list(steps)
    return list(_FALLBACK_INTERPRET_SKIP_STEPS)


def step_navigator_tuples() -> list[tuple[str, str, str]]:
    """StepNavigator 的 (step_key, num, label) 列表。"""
    raw = get_ui_strings().get("step_navigator", {}).get("steps")
    out: list[tuple[str, str, str]] = []
    if not isinstance(raw, list):
        return out
    for row in raw:
        if (
            isinstance(row, (list, tuple))
            and len(row) == 3
            and all(isinstance(x, str) for x in row)
        ):
            out.append((row[0], row[1], row[2]))
    return out
