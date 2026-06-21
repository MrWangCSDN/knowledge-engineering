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
# v2.0 工程状态机：段间回写 project.status（partial/ready/failed）+ 解读进度统计
from src.service.project_status_writer import update_project_status_sync
from src.service.db import get_session_maker
from src.pipeline.interpretation_progress_calc import interpretation_percent, methods_total
from src.pipeline.interpretation_standalone import get_interpretation_progress_from_weaviate

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
    # v2.0：多租户 project_id，优先从 run_pipeline(project_id=...) 取，
    # 次选 config.repo.project_id，最终 fallback "default"
    project_id: Optional[str] = None
    # CLI --force-full：清 Weaviate tenant + 删 embedding checkpoint，跑全量
    # 默认 False 是断点续跑
    force_full: bool = False

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

    def __post_init__(self) -> None:
        self.k = self.config.knowledge
        self.policy = InterpretationPipelinePolicy.from_knowledge_config(
            self.k,
            include_method_interpretation=self.include_method_interpretation,
        )
        self.repo_cfg = self.config.repo
        self.struct_cfg = self.config.structure
        # v2.0：解析有效 project_id（优先级：构造参数 > config.repo.project_id > "default"）
        if not self.project_id:
            self.project_id = (
                getattr(self.repo_cfg, "project_id", None)
                or "default"
            )

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
        layering=scope.struct_cfg.layering,     # 新增：透传分层配置（StructureConfig.layering → LayeringConfig）
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
        # v2.0：透传 project_id，写入 Neo4j 节点属性
        project_id=scope.project_id,
        # --force-full 透传：清 Weaviate tenant + 删 embedding checkpoint
        force_full=scope.force_full,
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
        step_callback=scope.step,
        progress_callback=scope.progress_callback,
        item_list_callback=scope.item_list_callback,
        item_completed_callback=scope.item_completed_callback,
        item_started_callback=scope.item_started_callback,
        interpretation_stats_callback=scope.interpretation_stats_callback,
        # v2.0：透传 project_id，写入 Weaviate tenant
        project_id=scope.project_id,
    )
    _execute_stages([InterpretationStage()], interpret_ctx)
    scope.interp_stats = interpret_ctx.interp_stats
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
        interp_stats=scope.interp_stats,
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
    ("finalize", _segment_finalize),
]


def execute_full_pipeline_table(scope: FullPipelineScope) -> dict[str, Any]:
    """按 `FULL_PIPELINE_SEGMENT_RUNNERS` 顺序执行；返回首个非 None 的字典（finalize 必返回）。

    v2.0：段循环里挂工程状态机回写（hook）：
    - semantic 段跑完 → status=partial（已有可用骨架，前端可放行问答）
    - finalize 段返回最终结果 → status=ready（带方法数/解读进度统计富化）
    - 任一段抛异常 → status=failed 后重新抛出
    - scope.project_id 为空时全程跳过，纯 CLI 跑零副作用
    """
    # getattr 安全取 project_id（即使 scope 不是标准 FullPipelineScope 也不报错）
    # 注：FullPipelineScope.__post_init__ 会把空 project_id 兜底成 "default"，故生产下 pid 几乎不为空。
    # 真正的"无副作用"保障来自 update_project_status 对不存在 project 行的 no-op，而非此处守卫。
    # 此处守卫只是 best-effort：pid 真为空（非标准 scope）时连 session maker 都不建。
    pid = getattr(scope, "project_id", None)
    # 只有需要回写时才构建 session maker，避免无 project_id 场景建库连接
    sm = get_session_maker() if pid else None

    def _mark(status: str, **kw: Any) -> None:
        """按 status 回写工程状态；pid/sm 缺失则静默跳过（CLI 无副作用）。"""
        # pid 空 或 sm 为 None → 不回写
        if pid and sm is not None:
            update_project_status_sync(sm, pid, status, **kw)

    try:
        # 保持原有循环：顺序执行段表，首个非 None 返回值即整条流水线结果
        for seg_id, runner in FULL_PIPELINE_SEGMENT_RUNNERS:
            out = runner(scope)
            # semantic 段一跑完就回写 partial（骨架已就绪）
            # 即使 until="semantic" 提前返回，partial 也应已写（语义上 semantic 确实完成了）
            if seg_id == "semantic":
                _mark("partial", interpretation_progress=0)
            if out is not None:
                # finalize 段产出最终结果时回写 ready，并富化统计
                if seg_id == "finalize":
                    # 统计富化可能触达 Weaviate/磁盘；用 try/except 降级：
                    # 即便拿不到统计，也要把 status 置位，不因统计失败中断流水线
                    try:
                        prog = get_interpretation_progress_from_weaviate(scope.config_path)
                        pct = interpretation_percent(prog)
                        # 结构+语义既已完成 ⟹ 至少 partial（绝不会回退到 indexing）；
                        # 解读覆盖率 100% 才置 ready，否则 partial（解读可能被门控跳过/部分完成）。
                        # 与 backfill 的 decide_status 在 total>0 正常情形下一致（满→ready / 未满→partial）；
                        # 二者仅在"无任何数据"时分歧（backfill 更保守判 indexing，因它没有"刚跑完一轮"信号）。
                        final_status = "ready" if pct >= 100 else "partial"
                        _mark(
                            final_status,
                            methods_count=methods_total(prog),
                            interpretation_progress=pct,
                            set_pipeline_at=True,
                        )
                    except Exception as e:  # noqa: BLE001 — 统计失败不应阻断状态置位
                        _LOG.warning(
                            "finalize 统计富化失败（已降级为仅置 ready）: %s: %s",
                            type(e).__name__,
                            e,
                        )
                        # 统计拿不到：pipeline 已完整跑完一轮，降级按 ready 置位
                        _mark("ready", set_pipeline_at=True)
                return out
        raise RuntimeError("流水线编排异常：所有段执行完毕但未返回结果")
    except Exception:
        # 任一段（或编排本身）异常 → 回写 failed 后重新抛出，不吞异常
        _mark("failed")
        raise
