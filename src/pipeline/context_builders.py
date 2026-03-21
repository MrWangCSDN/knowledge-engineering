"""各 Stage 上下文工厂函数。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from src.knowledge import KnowledgeGraph
from src.models import DomainKnowledge
from src.models.structure import StructureFacts
from src.core.context import AppContext
from src.core.domain_enums import InterpretPhase
from src.persistence.repositories.structure_facts_repository import StructureFactsRepository
from src.persistence.repositories.snapshot_repository import SnapshotRepository

from src.pipeline.stage_runtime import (
    FinalizeStageContext,
    InterpretationStageContext,
    KnowledgeStageContext,
    OntologyStageContext,
    SemanticStageContext,
    StructureStageContext,
)


def _build_structure_ctx(
    *,
    repo_path: str,
    repo_version: Optional[str],
    modules: list[Any],
    repo_language: Optional[str],
    extract_cross_service: bool,
    interpret_enabled: bool,
    progress_callback: Optional[Any],
    step_callback: Callable[[str], None],
    structure_repo: StructureFactsRepository,
    config_path: str | Path,
    out_dir: Optional[Path],
) -> StructureStageContext:
    return StructureStageContext(
        repo_path=repo_path,
        repo_version=repo_version,
        modules=modules,
        repo_language=repo_language,
        extract_cross_service=extract_cross_service,
        interpret_enabled=interpret_enabled,
        progress_callback=progress_callback,
        step_callback=step_callback,
        structure_repo=structure_repo,
        config_path=config_path,
        out_dir=out_dir,
    )


def _build_semantic_ctx(
    *,
    structure_facts: StructureFacts,
    domain: DomainKnowledge,
    out_dir: Optional[Path],
    step_callback: Callable[[str], None],
) -> SemanticStageContext:
    return SemanticStageContext(
        structure_facts=structure_facts,
        domain=domain,
        out_dir=out_dir,
        step_callback=step_callback,
    )


def _build_knowledge_ctx(
    *,
    structure_facts: StructureFacts,
    semantic_facts: Any,
    domain: DomainKnowledge,
    knowledge_cfg: Any,
    run_interpret_phase: bool,
    interpret_enabled: bool,
    progress_callback: Optional[Any],
    step_callback: Callable[[str], None],
    app_context: Optional[AppContext] = None,
) -> KnowledgeStageContext:
    return KnowledgeStageContext(
        structure_facts=structure_facts,
        semantic_facts=semantic_facts,
        domain=domain,
        knowledge_cfg=knowledge_cfg,
        run_interpret_phase=run_interpret_phase,
        interpret_enabled=interpret_enabled,
        progress_callback=progress_callback,
        step_callback=step_callback,
        app_context=app_context,
    )


def _build_interpretation_ctx(
    *,
    structure_facts: StructureFacts,
    domain: DomainKnowledge,
    knowledge_cfg: Any,
    run_interpret_phase: bool,
    want_interpret: bool,
    mi_on: bool,
    vinterp_on: bool,
    run_business_phase: bool,
    want_biz: bool,
    biz_capable: bool,
    step_callback: Callable[[str], None],
    progress_callback: Optional[Any],
    item_list_callback: Optional[Any],
    item_completed_callback: Optional[Any],
    item_started_callback: Optional[Callable[[str, InterpretPhase], None]],
    interpretation_stats_callback: Optional[Callable[[int, int, InterpretPhase], None]],
) -> InterpretationStageContext:
    return InterpretationStageContext(
        structure_facts=structure_facts,
        domain=domain,
        knowledge_cfg=knowledge_cfg,
        run_interpret_phase=run_interpret_phase,
        want_interpret=want_interpret,
        mi_on=mi_on,
        vinterp_on=vinterp_on,
        run_business_phase=run_business_phase,
        want_biz=want_biz,
        biz_capable=biz_capable,
        step_callback=step_callback,
        progress_callback=progress_callback,
        item_list_callback=item_list_callback,
        item_completed_callback=item_completed_callback,
        item_started_callback=item_started_callback,
        interpretation_stats_callback=interpretation_stats_callback,
        interp_stats={"skipped": True},
        biz_stats={"skipped": True},
    )


def _build_ontology_ctx(
    *,
    graph: KnowledgeGraph,
    out_dir: Optional[Path],
    knowledge_cfg: Any,
    step_callback: Callable[[str], None],
) -> OntologyStageContext:
    return OntologyStageContext(
        graph=graph,
        out_dir=out_dir,
        knowledge_cfg=knowledge_cfg,
        step_callback=step_callback,
        ontology_result=None,
    )


def _build_finalize_ctx(
    *,
    graph: KnowledgeGraph,
    out_dir: Optional[Path],
    knowledge_cfg: Any,
    repo_version: Optional[str],
    snapshot_repo: SnapshotRepository,
    structure_repo: StructureFactsRepository,
    structure_facts: StructureFacts,
    config_path: str | Path,
    ontology_result: Optional[dict[str, Any]],
    interp_stats: dict[str, Any],
    biz_stats: dict[str, Any],
    step_callback: Callable[[str], None],
) -> FinalizeStageContext:
    return FinalizeStageContext(
        graph=graph,
        out_dir=out_dir,
        knowledge_cfg=knowledge_cfg,
        repo_version=repo_version,
        snapshot_repo=snapshot_repo,
        structure_repo=structure_repo,
        structure_facts=structure_facts,
        config_path=config_path,
        ontology_result=ontology_result,
        interp_stats=interp_stats,
        biz_stats=biz_stats,
        step_callback=step_callback,
        result=None,
    )

