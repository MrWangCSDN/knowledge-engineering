"""流水线公共入口：完整 ``run_pipeline`` 及对外的向后兼容再导出。

实现细节见 ``stage_runtime``、``context_builders``、``interpretation_standalone``、``config_bootstrap``。
应用层宜优先使用 ``src.pipeline.gateways`` 窄接口，避免依赖本模块全部符号。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from src.core.context import AppContext
from src.core.domain_enums import InterpretPhase
from src.persistence.repositories.structure_facts_repository import (
    FileStructureFactsRepository,
    StructureFactsRepository,
)
from src.persistence.repositories.snapshot_repository import GraphSnapshotRepository, SnapshotRepository

from src.pipeline.config_bootstrap import config_to_domain, load_config
from src.pipeline.context_builders import (
    _build_finalize_ctx,
    _build_interpretation_ctx,
    _build_knowledge_ctx,
    _build_ontology_ctx,
    _build_semantic_ctx,
    _build_structure_ctx,
)
from src.pipeline.interpretation_standalone import (
    get_interpretation_progress_from_weaviate,
    run_interpretations_only,
    structure_facts_cache_path,
)
from src.pipeline.stage_runtime import (
    FinalizeStage,
    FinalizeStageContext,
    InterpretationStage,
    InterpretationStageContext,
    KnowledgeAwareStageContext,
    KnowledgeStage,
    KnowledgeStageContext,
    OntologyStage,
    OntologyStageContext,
    PipelineStage,
    SemanticStage,
    SemanticStageContext,
    StepStageContext,
    StructureStage,
    StructureStageContext,
    _execute_stages,
)

__all__ = [
    "load_config",
    "config_to_domain",
    "run_pipeline",
    "run_interpretations_only",
    "get_interpretation_progress_from_weaviate",
    "structure_facts_cache_path",
    "StepStageContext",
    "KnowledgeAwareStageContext",
    "InterpretationStageContext",
    "PipelineStage",
    "_execute_stages",
    "StructureStageContext",
    "StructureStage",
    "SemanticStageContext",
    "SemanticStage",
    "KnowledgeStageContext",
    "KnowledgeStage",
    "FinalizeStageContext",
    "FinalizeStage",
    "InterpretationStage",
    "OntologyStageContext",
    "OntologyStage",
    "_build_structure_ctx",
    "_build_semantic_ctx",
    "_build_knowledge_ctx",
    "_build_interpretation_ctx",
    "_build_ontology_ctx",
    "_build_finalize_ctx",
]


def run_pipeline(
    config_path: str | Path,
    until: Optional[str] = None,
    output_dir: Optional[str | Path] = None,
    progress_callback: Optional[Any] = None,
    step_callback: Optional[Any] = None,
    *,
    include_method_interpretation: Optional[bool] = None,
    include_business_interpretation: Optional[bool] = None,
    item_list_callback: Optional[Any] = None,
    item_completed_callback: Optional[Any] = None,
    item_started_callback: Optional[Callable[[str, InterpretPhase], None]] = None,
    interpretation_stats_callback: Optional[Callable[[int, int, InterpretPhase], None]] = None,
    structure_facts_repo: StructureFactsRepository | None = None,
    snapshot_repo: SnapshotRepository | None = None,
    app_context: AppContext | None = None,
) -> dict[str, Any]:
    """
    执行完整流水线：数据与触发 → 结构 → 语义 → 知识层。
    until: 可选 "structure" | "semantic" | "knowledge"，表示执行到该层后停止并输出中间结果。
    output_dir: 若指定，将 structure_facts / semantic_facts 写出为 JSON（便于调试）。
    执行到 knowledge 后会将配置与图写入 ``app_context``（省略时使用 ``AppContext`` 单例）。

    include_method_interpretation:
        None 时采用配置 knowledge.pipeline.include_method_interpretation_build；
        True 清空并 LLM 重建技术解读；False 仅重建图谱与代码向量，保留解读库。
    """
    config = load_config(config_path)
    app_ctx = app_context if app_context is not None else AppContext.get()
    app_ctx.set_config(config.model_dump())
    out_dir = Path(output_dir) if output_dir else None
    structure_repo = structure_facts_repo or FileStructureFactsRepository()
    snapshot_repo_impl = snapshot_repo or GraphSnapshotRepository()

    from src.pipeline.full_pipeline_orchestrator import FullPipelineScope, execute_full_pipeline_table

    scope = FullPipelineScope(
        config=config,
        config_path=config_path,
        out_dir=out_dir,
        until=until,
        structure_repo=structure_repo,
        snapshot_repo=snapshot_repo_impl,
        app_ctx=app_ctx,
        progress_callback=progress_callback,
        step_callback_raw=step_callback,
        item_list_callback=item_list_callback,
        item_completed_callback=item_completed_callback,
        item_started_callback=item_started_callback,
        interpretation_stats_callback=interpretation_stats_callback,
        include_method_interpretation=include_method_interpretation,
        include_business_interpretation=include_business_interpretation,
    )
    return execute_full_pipeline_table(scope)
