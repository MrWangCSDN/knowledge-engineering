"""流水线 Stage 运行时：各层上下文与 Stage 实现（不含 ``run_pipeline`` 入口）。"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from src.data_trigger import load_code_source
from src.structure import run_structure_layer
from src.semantic import run_semantic_layer
from src.knowledge import KnowledgeGraph
from src.knowledge.method_interpretation_runner import run_method_interpretations
from src.knowledge.business_interpretation_runner import run_business_interpretations
from src.knowledge.factories import GraphBackendFactory, VectorStoreFactory
from src.models import DomainKnowledge
from src.models.structure import StructureFacts
from src.core.context import AppContext
from src.core.domain_enums import InterpretPhase
from src.core.paths import pipeline_output_knowledge_snapshot_dir
from src.core.weaviate_defaults import (
    DEFAULT_COLLECTION_CODE_ENTITY,
    DEFAULT_WEAVIATE_GRPC_PORT,
    DEFAULT_WEAVIATE_HTTP_URL,
)

from src.persistence.repositories.structure_facts_repository import StructureFactsRepository
from src.persistence.repositories.snapshot_repository import SnapshotRepository

_LOG = logging.getLogger(__name__)

@dataclass
class StepStageContext:
    """Stage 公共上下文：统一步骤回调。"""

    step_callback: Callable[[str], None]


@dataclass
class KnowledgeAwareStageContext(StepStageContext):
    """Stage 公共上下文：统一知识配置访问。"""

    knowledge_cfg: Any


@dataclass
class InterpretationStageContext(KnowledgeAwareStageContext):
    """解读阶段上下文：封装执行参数与输出，便于以 stage 链组织。"""

    structure_facts: StructureFacts
    domain: DomainKnowledge
    run_interpret_phase: bool
    want_interpret: bool
    mi_on: bool
    vinterp_on: bool
    run_business_phase: bool
    want_biz: bool
    biz_capable: bool
    progress_callback: Optional[Any]
    item_list_callback: Optional[Any]
    item_completed_callback: Optional[Any]
    item_started_callback: Optional[Callable[[str, InterpretPhase], None]]
    interpretation_stats_callback: Optional[Callable[[int, int, InterpretPhase], None]]
    interp_stats: dict[str, Any]
    biz_stats: dict[str, Any]


class PipelineStage(Protocol):
    def execute(self, ctx: Any) -> None:
        ...


def _execute_stages(stages: list[PipelineStage], ctx: Any) -> None:
    """按顺序执行 stage 链。保持同步调用，不引入并发语义变化。"""
    for stage in stages:
        stage.execute(ctx)


@dataclass
class StructureStageContext(StepStageContext):
    """结构层上下文。"""

    repo_path: str
    repo_version: Optional[str]
    modules: list[Any]
    repo_language: Optional[str]
    extract_cross_service: bool
    interpret_enabled: bool
    progress_callback: Optional[Any]
    structure_repo: StructureFactsRepository
    config_path: str | Path
    out_dir: Optional[Path]
    source: Any = None
    structure_facts: Optional[StructureFacts] = None


class StructureStage:
    """结构层：触发加载代码并解析 AST。"""

    def execute(self, ctx: StructureStageContext) -> None:
        ctx.source = load_code_source(
            repo_path=ctx.repo_path,
            version=ctx.repo_version,
            modules=ctx.modules,
            language=ctx.repo_language,
        )
        ctx.step_callback("① 结构层：解析 AST …")

        def _wrap_struct_progress(cb):
            if cb is None:
                return None

            def _inner(cur, tot, msg):
                frac = (cur / tot) if tot else 0.0
                cap = 22 if ctx.interpret_enabled else 28
                cb(int(frac * cap), 100, msg)

            return _inner

        ctx.structure_facts = run_structure_layer(
            ctx.source,
            extract_cross_service=ctx.extract_cross_service,
            progress_callback=_wrap_struct_progress(ctx.progress_callback),
        )
        ctx.step_callback("② 结构层完成")

        ctx.structure_repo.save(
            ctx.structure_facts,
            config_path=ctx.config_path,
            out_dir=ctx.out_dir,
            write_cache=False,
        )
        try:
            ctx.structure_repo.save(ctx.structure_facts, config_path=ctx.config_path, write_cache=True)
        except OSError as e:
            _LOG.warning("结构层：写入结构事实缓存失败（已忽略）: %s", e)
        except Exception as e:
            _LOG.error(
                "结构层：写入结构事实缓存未预期失败（已忽略）: %s: %s",
                type(e).__name__,
                e,
                exc_info=True,
            )


@dataclass
class SemanticStageContext(StepStageContext):
    """语义层上下文。"""

    structure_facts: StructureFacts
    domain: DomainKnowledge
    out_dir: Optional[Path]
    semantic_facts: Any = None


class SemanticStage:
    """语义层：术语/向量文本构建。"""

    def execute(self, ctx: SemanticStageContext) -> None:
        ctx.step_callback("③ 语义层：术语与向量文本 …")
        ctx.semantic_facts = run_semantic_layer(
            ctx.structure_facts,
            ctx.domain,
            enable_vector_text=True,
        )
        ctx.step_callback("④ 语义层完成")

        if ctx.out_dir:
            (ctx.out_dir / "semantic_facts.json").write_text(
                ctx.semantic_facts.model_dump_json(indent=2, exclude_none=True),
                encoding="utf-8",
            )


@dataclass
class KnowledgeStageContext(KnowledgeAwareStageContext):
    """知识层上下文。

    ``app_context``：图构建完成后 ``set_graph`` 的目标；为 ``None`` 时使用 ``AppContext.get()``。
    """

    structure_facts: StructureFacts
    semantic_facts: Any
    domain: DomainKnowledge
    run_interpret_phase: bool
    interpret_enabled: bool
    progress_callback: Optional[Any]
    graph: Optional[KnowledgeGraph] = None
    app_context: Optional[AppContext] = None


class KnowledgeStage:
    """知识层：清理后端 + 构建内存图 + 同步全局图。"""

    def execute(self, ctx: KnowledgeStageContext) -> None:
        k = ctx.knowledge_cfg
        graph_cfg = k.graph
        vector_cfg = k.vectordb_code
        graph_backend = graph_cfg.backend
        ctx.step_callback(
            "⑤ 清理 Neo4j（Entity）与 Weaviate 代码库"
            + (" + 技术解读库 …" if ctx.run_interpret_phase else "（保留技术解读库）…")
        )

        if graph_backend == "neo4j":
            try:
                backend = GraphBackendFactory.create(
                    "neo4j",
                    neo4j_uri=graph_cfg.neo4j_uri or "bolt://localhost:7687",
                    neo4j_user=graph_cfg.neo4j_user or "neo4j",
                    neo4j_password=graph_cfg.neo4j_password or "password",
                    neo4j_database=graph_cfg.neo4j_database or "neo4j",
                )
                backend.clear()
                backend.close()
            except (OSError, ConnectionError, TimeoutError, ValueError, RuntimeError) as e:
                _LOG.warning(
                    "知识层：清理 Neo4j 失败（已忽略）: %s: %s",
                    type(e).__name__,
                    e,
                )
            except Exception as e:
                _LOG.error(
                    "知识层：清理 Neo4j 未预期异常（已忽略）: %s: %s",
                    type(e).__name__,
                    e,
                    exc_info=True,
                )

        if vector_cfg.enabled and vector_cfg.backend == "weaviate":
            vs = None
            try:
                vs = VectorStoreFactory.create(
                    vector_cfg.backend,
                    True,
                    vector_cfg.dimension,
                    allow_fallback_to_memory=bool(vector_cfg.allow_fallback_to_memory),
                    weaviate_url=vector_cfg.weaviate_url or DEFAULT_WEAVIATE_HTTP_URL,
                    weaviate_grpc_port=vector_cfg.weaviate_grpc_port or DEFAULT_WEAVIATE_GRPC_PORT,
                    collection_name=vector_cfg.collection_name or DEFAULT_COLLECTION_CODE_ENTITY,
                    weaviate_api_key=vector_cfg.weaviate_api_key,
                )
                if vs is not None:
                    vs.clear()
            except (OSError, ConnectionError, TimeoutError, ValueError, RuntimeError) as e:
                _LOG.warning(
                    "知识层：清理 Weaviate 代码向量库失败（已忽略）: %s: %s",
                    type(e).__name__,
                    e,
                )
            except Exception as e:
                _LOG.error(
                    "知识层：清理 Weaviate 代码向量库未预期异常（已忽略）: %s: %s",
                    type(e).__name__,
                    e,
                    exc_info=True,
                )
            finally:
                if vs is not None and hasattr(vs, "close"):
                    try:
                        vs.close()
                    except OSError as e:
                        _LOG.warning("知识层：关闭代码向量存储失败（已忽略）: %s", e)
                    except Exception as e:
                        _LOG.error(
                            "知识层：关闭代码向量存储未预期异常（已忽略）: %s: %s",
                            type(e).__name__,
                            e,
                            exc_info=True,
                        )

        def _wrap_graph_progress(cb):
            if cb is None:
                return None

            lo, hi = (22, 78) if ctx.interpret_enabled else (22, 100)

            def _inner(cur, tot, msg):
                frac = (cur / tot) if tot else 0.0
                cb(int(lo + frac * (hi - lo)), 100, msg)

            return _inner

        ctx.step_callback("⑥ 知识层：构建内存图、写入代码向量、同步 Neo4j …")
        graph = KnowledgeGraph()
        graph.build_from(
            ctx.structure_facts,
            ctx.semantic_facts,
            ctx.domain,
            vector_enabled=vector_cfg.enabled,
            vector_dim=vector_cfg.dimension,
            graph_backend=graph_backend,
            vector_backend=vector_cfg.backend,
            graph_config=k.to_graph_dict(),
            vector_config=k.to_vectordb_code_dict(),
            progress_callback=_wrap_graph_progress(ctx.progress_callback),
        )
        _actx = ctx.app_context if ctx.app_context is not None else AppContext.get()
        _actx.set_graph(graph)
        ctx.step_callback("⑦ 知识层与代码向量库已完成")
        if ctx.progress_callback:
            ctx.progress_callback(
                78 if ctx.interpret_enabled else 100,
                100,
                "准备技术解读（将调用 LLM）…" if ctx.interpret_enabled else "流水线主体完成（已跳过技术解读，解读库未清空）",
            )
        ctx.graph = graph


@dataclass
class FinalizeStageContext(KnowledgeAwareStageContext):
    """收尾阶段上下文：统计、快照、消息与返回值。"""

    graph: KnowledgeGraph
    out_dir: Optional[Path]
    repo_version: Optional[str]
    snapshot_repo: SnapshotRepository
    structure_repo: StructureFactsRepository
    structure_facts: StructureFacts
    config_path: str | Path
    ontology_result: Optional[dict[str, Any]]
    interp_stats: dict[str, Any]
    biz_stats: dict[str, Any]
    result: Optional[dict[str, Any]] = None


class FinalizeStage:
    """流水线收尾：输出统计、生成消息、组织返回结构。"""

    def execute(self, ctx: FinalizeStageContext) -> None:
        ctx.step_callback("⑩ 流水线全部结束")
        k = ctx.knowledge_cfg
        graph = ctx.graph
        if ctx.out_dir and graph:
            stats = {"nodes": graph.node_count(), "edges": graph.edge_count()}
            (ctx.out_dir / "knowledge_stats.json").write_text(
                json.dumps(stats, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            if k.snapshot.save_after_build:
                snap_dir = pipeline_output_knowledge_snapshot_dir(ctx.out_dir)
                version = ctx.repo_version or "default"
                ctx.snapshot_repo.save(graph, snap_dir, version=version)

        neo4j_status = getattr(graph, "_neo4j_sync_status", None)
        msg = "知识图谱已构建，服务层 API 已可用"
        if neo4j_status == "ok":
            msg += "；已同步到 Neo4j"
        elif neo4j_status and str(neo4j_status).startswith("failed:"):
            msg += f"；Neo4j 同步失败: {str(neo4j_status)[7:]}"
        elif neo4j_status == "skipped":
            msg += "；未配置 Neo4j（knowledge.graph.backend 非 neo4j）"
        elif neo4j_status is None:
            msg += "；Neo4j 同步未执行"
        if ctx.ontology_result and not ctx.ontology_result.get("errors"):
            msg += f"；OWL 推理完成（推断边 {ctx.ontology_result.get('inferred_count', 0)} 条，写回 {ctx.ontology_result.get('written_to_graph', 0)} 条）"
        if ctx.interp_stats.get("mode") == "graph_and_code_only":
            msg += "；技术解读库已保留（未重建）"
        elif ctx.interp_stats.get("mode") == "interpret_config_disabled":
            msg += "；技术解读未执行（请启用 method_interpretation 与 vectordb-interpret）"
        elif not ctx.interp_stats.get("skipped"):
            msg += f"；技术解读写入 {ctx.interp_stats.get('written', 0)} 条（失败/跳过 {ctx.interp_stats.get('failed', 0)}）"
        if not ctx.biz_stats.get("skipped") and ctx.biz_stats.get("candidates_class") is not None:
            msg += (
                f"；业务解读本轮 {ctx.biz_stats.get('written', 0)} 条"
                f"（类 {ctx.biz_stats.get('todo_this_run_class', 0)}，API {ctx.biz_stats.get('todo_this_run_api', 0)}，模块 {ctx.biz_stats.get('todo_this_run_module', 0)}）"
            )

        try:
            ctx.structure_repo.save(ctx.structure_facts, config_path=ctx.config_path, write_cache=True)
        except OSError as e:
            _LOG.warning("收尾：写入结构事实缓存失败（已忽略）: %s", e)
        except Exception as e:
            _LOG.error(
                "收尾：写入结构事实缓存未预期失败（已忽略）: %s: %s",
                type(e).__name__,
                e,
                exc_info=True,
            )

        result = {
            "stage": "knowledge",
            "graph_nodes": graph.node_count(),
            "graph_edges": graph.edge_count(),
            "vector_store_size": graph._vector_store.size() if graph._vector_store else 0,
            "neo4j_sync": neo4j_status,
            "message": msg,
            "interpretation": ctx.interp_stats,
            "business_interpretation": ctx.biz_stats,
        }
        if ctx.ontology_result is not None:
            result["ontology"] = ctx.ontology_result
        ctx.result = result


class InterpretationStage:
    """技术解读 + 业务解读阶段（最小拆分，不改变现有回调时序）。"""

    def execute(self, ctx: InterpretationStageContext) -> None:
        k = ctx.knowledge_cfg
        if ctx.run_interpret_phase:
            ctx.step_callback("⑦′ 技术解读：调用 LLM …")
            ctx.interp_stats = run_method_interpretations(
                ctx.structure_facts,
                k.method_interpretation,
                k.vectordb_interpret,
                step_callback=ctx.step_callback,
                progress_callback=ctx.progress_callback,
                item_list_callback=ctx.item_list_callback,
                item_completed_callback=ctx.item_completed_callback,
                item_started_callback=ctx.item_started_callback,
                interpretation_stats_callback=ctx.interpretation_stats_callback,
            )
        elif ctx.want_interpret and not (ctx.mi_on and ctx.vinterp_on):
            ctx.step_callback("⑦′ 技术解读：已请求执行，但配置未同时启用 method_interpretation 与 vectordb-interpret，已跳过。")
            ctx.interp_stats = {"skipped": True, "written": 0, "failed": 0, "mode": "interpret_config_disabled"}
        else:
            ctx.step_callback("⑦′ 技术解读：「仅图谱+代码」— 已保留 Weaviate 解读库，未调用 LLM。")
            ctx.interp_stats = {"skipped": True, "written": 0, "failed": 0, "mode": "graph_and_code_only", "interpret_preserved": True}

        if ctx.run_business_phase:
            ctx.step_callback("⑦· 业务解读：类/接口、API 用例、模块综述（增量续跑）…")
            try:
                ctx.biz_stats = run_business_interpretations(
                    ctx.structure_facts,
                    ctx.domain,
                    k.business_interpretation,
                    k.vectordb_business,
                    step_callback=ctx.step_callback,
                    progress_callback=ctx.progress_callback,
                    item_list_callback=ctx.item_list_callback,
                    item_completed_callback=ctx.item_completed_callback,
                    item_started_callback=ctx.item_started_callback,
                    interpretation_stats_callback=ctx.interpretation_stats_callback,
                )
            except Exception as e:
                ctx.biz_stats = {"skipped": True, "error": repr(e)}
                ctx.step_callback(f"业务解读阶段异常，已跳过：{e!r}")
        elif ctx.want_biz and not ctx.biz_capable:
            ctx.biz_stats = {"skipped": True, "mode": "business_config_disabled"}
        else:
            ctx.biz_stats = {"skipped": True, "mode": "business_not_requested"}


@dataclass
class OntologyStageContext(KnowledgeAwareStageContext):
    """OWL 阶段上下文：最小抽离，不改变既有回调与输出语义。"""

    graph: KnowledgeGraph
    out_dir: Optional[Path]
    ontology_result: Optional[dict[str, Any]]


class OntologyStage:
    """OWL 本体推理阶段。"""

    def execute(self, ctx: OntologyStageContext) -> None:
        ont_cfg = ctx.knowledge_cfg.ontology
        if ont_cfg.enabled:
            ctx.step_callback("⑧ OWL 推理与写回 …")
            try:
                from src.knowledge.ontology import run_ontology_pipeline

                export_path = None
                if ctx.out_dir and ont_cfg.export_after_build:
                    export_path = ctx.out_dir / "knowledge_ontology.ttl"
                ctx.ontology_result = run_ontology_pipeline(
                    ctx.graph,
                    export_owl=ont_cfg.export_owl,
                    export_path=export_path,
                    run_reasoner=ont_cfg.reasoner,
                    write_inferred_to_graph=ont_cfg.write_inferred_to_graph,
                )
            except ImportError as e:
                ctx.ontology_result = {"errors": [f"未安装 OWL 依赖: {e!r}，请执行: pip install -e '.[owl]'"]}
            except Exception as e:
                ctx.ontology_result = {"errors": [f"本体推理失败: {e!r}"]}
            ctx.step_callback("⑨ OWL 步骤结束")
        else:
            ctx.step_callback("⑧⑨ OWL 未启用，已跳过")

