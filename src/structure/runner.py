"""结构层入口：从 CodeInputSource 产出 StructureFacts。"""
from __future__ import annotations

from typing import Any, Optional

from src.models import CodeInputSource, StructureFacts
from .java_parser import JavaStructureExtractor


def run_structure_layer(
    source: CodeInputSource,
    extract_cross_service: bool = True,
    progress_callback: Optional[Any] = None,
) -> StructureFacts:
    """
    对 source 中的文件做 AST 解析与结构抽取，输出与语言无关的结构事实。
    当前实现：仅 Java（javalang）；跨服务抽取为可选。
    """
    language = (source.language or "java").lower()
    if language != "java":
        # 占位：其他语言可在此扩展
        return StructureFacts(meta={"language": language, "message": "仅支持 java，其余返回空"})

    extractor = JavaStructureExtractor(
        repo_path=source.repo_path,
        extract_cross_service=extract_cross_service,
    )
    # JavaStructureExtractor 目前未显式暴露细粒度进度，这里先把「开始结构解析」告知上层，
    # 后续可在 extractor 内部进一步细化（如按文件数）。
    if progress_callback:
        progress_callback(0, 1, "正在解析代码结构（AST）…")
    facts = extractor.extract(source)
    if progress_callback:
        progress_callback(1, 1, "代码结构解析完成。")
    return facts
