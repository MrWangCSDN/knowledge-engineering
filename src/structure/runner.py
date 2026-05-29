"""结构层入口：从 CodeInputSource 产出 StructureFacts。

使用 JavaParser Bridge (Java 1-25+) 解析 Java 源码；
+ 用 MyBatisXmlExtractor 解析 Mapper XML（设计 [[MyBatis-XML-Extractor-设计]]）。
"""
from __future__ import annotations

import logging
from pathlib import Path
# typing.TYPE_CHECKING 是一个特殊常量：运行时始终为 False，仅在类型检查工具（如 mypy/pyright）分析时为 True
# 用它包裹的 import 只在静态分析期生效，运行时不会执行，从而避免循环依赖
from typing import TYPE_CHECKING, Any, Optional

from src.models import CodeInputSource, StructureFacts

# TYPE_CHECKING 块：只有类型检查器才会「看到」这段 import；运行时跳过，不产生循环导入风险
if TYPE_CHECKING:
    # LayeringConfig 是 Pydantic 配置模型，描述架构分层采集的全部参数
    from src.config.models import LayeringConfig

_LOG = logging.getLogger(__name__)


def run_structure_layer(
    source: CodeInputSource,
    extract_cross_service: bool = True,
    progress_callback: Optional[Any] = None,
    layering: "LayeringConfig | None" = None,   # 新增：架构分层配置；None = 跳过分层（向后兼容）
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

    # ── 新增：架构分层打标签（设计 [[架构分层采集-设计]]）────────────────────
    # 使用延迟 import（在函数内部才 import），而非顶层 import
    # 这样可以避免模块加载时产生循环依赖（apply 模块也可能 import structure 相关类）
    from src.structure.layering.apply import apply_layering   # 延迟 import，规避顶层循环依赖

    # apply_layering(facts, layering)：
    #   - 若 layering 为 None 或 enabled=False，内部直接返回 {"skipped": True}，不修改任何实体
    #   - 否则对 facts.entities 中每个实体调用 classify，把层 id 写入 entity.attributes["layer"]
    layer_stats = apply_layering(facts, layering)

    # 只在实际执行了分层（非跳过）时打日志，避免每次 build 都输出噪音
    if not layer_stats.get("skipped"):
        _LOG.info("[structure] 架构分层：打标签 %d 个实体", layer_stats.get("applied", 0))

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
