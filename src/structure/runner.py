"""结构层入口：从 CodeInputSource 产出 StructureFacts。

使用 JavaParser Bridge (Java 1-25+) 解析 Java 源码。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from src.models import CodeInputSource, StructureFacts

_LOG = logging.getLogger(__name__)


def run_structure_layer(
    source: CodeInputSource,
    extract_cross_service: bool = True,
    progress_callback: Optional[Any] = None,
) -> StructureFacts:
    """
    对 source 中的文件做 AST 解析与结构抽取，输出与语言无关的结构事实。
    使用 JavaParser Bridge（支持 Java 1-25+）。
    """
    language = (source.language or "java").lower()
    if language != "java":
        return StructureFacts(meta={"language": language, "message": "仅支持 java，其余返回空"})

    from .javaparser_bridge import run_javaparser_bridge

    if progress_callback:
        progress_callback(0, 1, "正在解析代码结构（JavaParser, Java 1-25+）…")

    facts = run_javaparser_bridge(
        source=source,
        extract_cross_service=extract_cross_service,
        progress_callback=progress_callback,
    )

    if progress_callback:
        progress_callback(1, 1, f"代码结构解析完成（{len(facts.entities)} 实体, {len(facts.relations)} 关系）")

    return facts
