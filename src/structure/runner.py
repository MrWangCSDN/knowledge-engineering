"""结构层入口：从 CodeInputSource 产出 StructureFacts。

使用 JavaParser Bridge (Java 1-25+) 解析 Java 源码；
+ 用 MyBatisXmlExtractor 解析 Mapper XML（设计 [[MyBatis-XML-Extractor-设计]]）。
"""
from __future__ import annotations

import logging
from pathlib import Path
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
    使用 JavaParser Bridge（支持 Java 1-25+）+ MyBatisXmlExtractor。
    """
    language = (source.language or "java").lower()
    if language != "java":
        return StructureFacts(meta={"language": language, "message": "仅支持 java，其余返回空"})

    from .javaparser_bridge import run_javaparser_bridge

    if progress_callback:
        progress_callback(0, 3, "正在解析 Java 代码（JavaParser, Java 1-25+）…")

    facts = run_javaparser_bridge(
        source=source,
        extract_cross_service=extract_cross_service,
        progress_callback=progress_callback,
    )

    # ── 新增：扫 Mapper XML（设计 §3）────────────────────────────────────
    if progress_callback:
        progress_callback(1, 3, "正在解析 MyBatis Mapper XML…")
    xml_added = _extract_mybatis_xml(source, facts)
    _LOG.info("[structure] MyBatis XML 解析：新增 %d 个 entity", xml_added)

    # ── 新增：跨文件 link Java method ↔ XML statement（设计 §4）─────────
    if progress_callback:
        progress_callback(2, 3, "正在关联 Java method ↔ XML statement…")
    link_added = _link_java_to_mybatis_xml(facts)
    _LOG.info("[structure] MyBatis Java↔XML 合成边：%d 条", link_added)

    if progress_callback:
        progress_callback(3, 3,
            f"代码结构解析完成（{len(facts.entities)} 实体, {len(facts.relations)} 关系）")

    return facts


def _extract_mybatis_xml(source: CodeInputSource, facts: StructureFacts) -> int:
    """扫 source.repo_path 下所有 .xml，跑 MyBatisXmlExtractor。

    :returns: 新增的 entity 数（含 file + method）
    """
    from .mybatis_extractor import MyBatisXmlExtractor

    # CodeInputSource 的路径字段名按实际 schema 拿（可能是 repo_path 或 path）
    repo_path = getattr(source, "repo_path", None) or getattr(source, "path", None)
    if not repo_path:
        _LOG.warning("[structure] source 无 repo_path/path，跳过 MyBatis XML 扫描")
        return 0

    root = Path(repo_path)
    if not root.exists():
        _LOG.warning("[structure] repo_path 不存在：%s", repo_path)
        return 0

    added = 0
    for xml_path in root.rglob("*.xml"):
        rel = xml_path.relative_to(root)
        parts = rel.parts
        # 跳过常见非源码目录
        if any(p in ("target", "build", "node_modules", ".git", "dist") for p in parts):
            continue
        try:
            content = xml_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            _LOG.warning("[structure] 读 XML 失败 %s: %s", xml_path, e)
            continue

        rel_str = str(rel).replace("\\", "/")
        extractor = MyBatisXmlExtractor(rel_str, content)
        result = extractor.extract()

        facts.entities.extend(result.entities)
        facts.relations.extend(result.relations)
        added += len(result.entities)

        if result.errors:
            for err in result.errors:
                _LOG.warning("[structure] mybatis_extractor error in %s: %s",
                             rel_str, err.get("message"))

    return added


def _link_java_to_mybatis_xml(facts: StructureFacts) -> int:
    """跑 mybatis_link_resolver，把合成的 CALLS edges 追加到 facts.relations。"""
    from .mybatis_link_resolver import synthesize_mybatis_java_xml_relations

    new_edges = synthesize_mybatis_java_xml_relations(facts)
    facts.relations.extend(new_edges)
    return len(new_edges)
