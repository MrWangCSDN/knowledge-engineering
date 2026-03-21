"""完整流水线表驱动编排：段表 + FullPipelineScope，与现有 Stage/Context 对齐。

``run_pipeline`` 负责加载配置、校验 repo，再构建 `FullPipelineScope` 并调用
`execute_full_pipeline_table`；具体段逻辑在此模块，便于单测与扩展新段。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from src.core.context import AppContext
from src.config import ProjectConfig
from src.core.domain_enums import InterpretPhase
from src.knowledge import KnowledgeGraph
from src.models import DomainKnowledge
from src.models.structure import StructureFacts
from src.persistence.repositories.structure_facts_repository import StructureFactsRepository
from src.persistence.repositories.snapshot_repository import SnapshotRepository
from src.pipeline.interpretation_policy import InterpretationPipelinePolicy

_LOG = logging.getLogger(__name__)


@dataclass
class FullPipelineScope:
    """跨流水线段共享的输入、派生标志与可变产物。"""

    config: ProjectConfig
    config_path: str | Path
    out_dir: Optional[Path]
    until: Optional[str]
    structure_repo: StructureFactsRepository
    snapshot_repo: SnapshotRepository
    app_ctx: AppContext
    progress_callback: Optional[Any]
    step_callback_raw: Optional[Any]
    item_list_callback: Optional[Any]
    item_completed_callback: Optional[Any]
    item_started_callback: Optional[Callable[[str, InterpretPhase], None]]
    interpretation_stats_callback: Optional[Callable[[int, int, InterpretPhase], None]]
    include_method_interpretation: Optional[bool]
    include_business_interpretation: Optional[bool]

    # --- 派生（__post_init__）---
    k: Any = field(init=False)
    policy: InterpretationPipelinePolicy = field(init=False)
    repo_cfg: Any = field(init=False)
    struct_cfg: Any = field(init=False)

    # --- 段间可变状态 ---
    source: Any = None
    structure_facts: Optional[StructureFacts] = None
    domain: Optional[DomainKnowledge] = None
    semantic_facts: Any = None
    graph: Optional[KnowledgeGraph] = None
    interp_stats: dict[str, Any] = field(default_factory=lambda: {"skipped": True})
    biz_stats: dict[str, Any] = field(default_factory=lambda: {"skipped": True})
    ontology_result: Optional[dict[str, Any]] = None

    def __post_init__(self) -> None:
        self.k = self.config.knowledge
        self.policy = InterpretationPipelinePolicy.from_knowledge_config(
            self.k,
            include_method_interpretation=self.include_method_interpretation,
            include_business_interpretation=self.include_business_interpretation,
        )
        self.repo_cfg = self.config.repo
        self.struct_cfg = self.config.structure

    def step(self, msg: str) -> None:
        if self.step_callback_raw:
            try:
                self.step_callback_raw(msg)
            except Exception as e:
                _LOG.debug(
                    "run_pipeline: step_callback 失败（已忽略）: %s: %s",
                    type(e).__name__,
                    e,
                    exc_info=True,
                )


# 延迟绑定：段函数内 import run 中的 Stage 与 builder（run 模块已加载后再 import）
def _segment_structure(scope: FullPipelineScope) -> Optional[dict[str, Any]]:
    from src.pipeline.context_builders import _build_structure_ctx
    from src.pipeline.stage_runtime import StructureStage, _execute_stages

    repo_path = scope.repo_cfg.path or ""
    if not repo_path or not Path(repo_path).is_dir():
        raise FileNotFoundError(
            f"请在配置中设置 repo.path 为有效的本地代码库路径，当前: {repo_path}"
        )
    modules = [m.model_dump() if hasattr(m, "model_dump") else m for m in scope.repo_cfg.modules]
    structure_ctx = _build_structure_ctx(
        repo_path=repo_path,
        repo_version=scope.repo_cfg.version,
        modules=modules,
        repo_language=scope.repo_cfg.language,
        extract_cross_service=scope.struct_cfg.extract_cross_service,
        interpret_enabled=scope.policy.interpret_enabled,
        progress_callback=scope.progress_callback,
        step_callback=scope.step,
        structure_repo=scope.structure_repo,
        config_path=scope.config_path,
        out_dir=scope.out_dir,
    )
    _execute_stages([StructureStage()], structure_ctx)
    scope.source = structure_ctx.source
    scope.structure_facts = structure_ctx.structure_facts
    if scope.structure_facts is None:
        raise RuntimeError("结构层执行失败：未生成 structure_facts")

    if scope.until == "structure":
        return {"stage": "structure", "structure_facts": scope.structure_facts, "source": scope.source}
    return None


def _segment_semantic(scope: FullPipelineScope) -> Optional[dict[str, Any]]:
    from src.pipeline.config_bootstrap import config_to_domain
    from src.pipeline.context_builders import _build_semantic_ctx
    from src.pipeline.stage_runtime import SemanticStage, _execute_stages

    scope.domain = config_to_domain(scope.config)
    semantic_ctx = _build_semantic_ctx(
        structure_facts=scope.structure_facts,  # type: ignore[arg-type]
        domain=scope.domain,
        out_dir=scope.out_dir,
        step_callback=scope.step,
    )
    _execute_stages([SemanticStage()], semantic_ctx)
    scope.semantic_facts = semantic_ctx.semantic_facts

    if scope.until == "semantic":
        return {
            "stage": "semantic",
            "semantic_facts": scope.semantic_facts,
            "structure_facts": scope.structure_facts,
        }
    return None


def _segment_knowledge(scope: FullPipelineScope) -> Optional[dict[str, Any]]:
    from src.pipeline.context_builders import _build_knowledge_ctx
    from src.pipeline.stage_runtime import KnowledgeStage, _execute_stages

    knowledge_ctx = _build_knowledge_ctx(
        structure_facts=scope.structure_facts,  # type: ignore[arg-type]
        semantic_facts=scope.semantic_facts,
        domain=scope.domain,  # type: ignore[arg-type]
        knowledge_cfg=scope.k,
        run_interpret_phase=scope.policy.run_interpret_phase,
        interpret_enabled=scope.policy.interpret_enabled,
        progress_callback=scope.progress_callback,
        step_callback=scope.step,
        app_context=scope.app_ctx,
    )
    _execute_stages([KnowledgeStage()], knowledge_ctx)
    scope.graph = knowledge_ctx.graph
    if scope.graph is None:
        raise RuntimeError("知识层执行失败：未生成 graph")
    return None


def _segment_interpretation(scope: FullPipelineScope) -> Optional[dict[str, Any]]:
    from src.pipeline.context_builders import _build_interpretation_ctx
    from src.pipeline.stage_runtime import InterpretationStage, _execute_stages

    interpret_ctx = _build_interpretation_ctx(
        structure_facts=scope.structure_facts,  # type: ignore[arg-type]
        domain=scope.domain,  # type: ignore[arg-type]
        knowledge_cfg=scope.k,
        run_interpret_phase=scope.policy.run_interpret_phase,
        want_interpret=scope.policy.want_interpret,
        mi_on=scope.policy.mi_on,
        vinterp_on=scope.policy.vinterp_on,
        run_business_phase=scope.policy.run_business_phase,
        want_biz=scope.policy.want_biz,
        biz_capable=scope.policy.biz_capable,
        step_callback=scope.step,
        progress_callback=scope.progress_callback,
        item_list_callback=scope.item_list_callback,
        item_completed_callback=scope.item_completed_callback,
        item_started_callback=scope.item_started_callback,
        interpretation_stats_callback=scope.interpretation_stats_callback,
    )
    _execute_stages([InterpretationStage()], interpret_ctx)
    scope.interp_stats = interpret_ctx.interp_stats
    scope.biz_stats = interpret_ctx.biz_stats
    return None


def _segment_ontology(scope: FullPipelineScope) -> Optional[dict[str, Any]]:
    from src.pipeline.context_builders import _build_ontology_ctx
    from src.pipeline.stage_runtime import OntologyStage, _execute_stages

    ontology_ctx = _build_ontology_ctx(
        graph=scope.graph,  # type: ignore[arg-type]
        out_dir=scope.out_dir,
        knowledge_cfg=scope.k,
        step_callback=scope.step,
    )
    _execute_stages([OntologyStage()], ontology_ctx)
    scope.ontology_result = ontology_ctx.ontology_result
    return None


def _segment_finalize(scope: FullPipelineScope) -> Optional[dict[str, Any]]:
    from src.pipeline.context_builders import _build_finalize_ctx
    from src.pipeline.stage_runtime import FinalizeStage, _execute_stages

    finalize_ctx = _build_finalize_ctx(
        graph=scope.graph,  # type: ignore[arg-type]
        out_dir=scope.out_dir,
        knowledge_cfg=scope.k,
        repo_version=scope.repo_cfg.version,
        snapshot_repo=scope.snapshot_repo,
        structure_repo=scope.structure_repo,
        structure_facts=scope.structure_facts,  # type: ignore[arg-type]
        config_path=scope.config_path,
        ontology_result=scope.ontology_result,
        interp_stats=scope.interp_stats,
        biz_stats=scope.biz_stats,
        step_callback=scope.step,
    )
    _execute_stages([FinalizeStage()], finalize_ctx)
    if finalize_ctx.result is None:
        raise RuntimeError("收尾阶段执行失败：未生成 result")
    return finalize_ctx.result


# 表驱动：顺序执行；任一段返回非 None dict 则整条流水线结束（提前返回或最终结果）
FULL_PIPELINE_SEGMENT_RUNNERS: list[tuple[str, Callable[[FullPipelineScope], Optional[dict[str, Any]]]]] = [
    ("structure", _segment_structure),
    ("semantic", _segment_semantic),
    ("knowledge", _segment_knowledge),
    ("interpretation", _segment_interpretation),
    ("ontology", _segment_ontology),
    ("finalize", _segment_finalize),
]


def execute_full_pipeline_table(scope: FullPipelineScope) -> dict[str, Any]:
    """按 `FULL_PIPELINE_SEGMENT_RUNNERS` 顺序执行；返回首个非 None 的字典（finalize 必返回）。"""
    for seg_id, runner in FULL_PIPELINE_SEGMENT_RUNNERS:
        out = runner(scope)
        if out is not None:
            return out
    raise RuntimeError("流水线编排异常：所有段执行完毕但未返回结果")
