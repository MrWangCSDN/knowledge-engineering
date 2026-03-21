"""独立解读流程：Weaviate 进度查询与「仅解读」流水线（不经由完整 Stage 表）。"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Optional

from src.core.context import AppContext
from src.core.domain_enums import InterpretPhase
from src.core.paths import (
    structure_facts_interpret_cache_display_path,
    structure_facts_interpret_cache_path_from_config,
)
from src.core.weaviate_defaults import (
    DEFAULT_COLLECTION_BUSINESS_INTERPRETATION,
    DEFAULT_COLLECTION_METHOD_INTERPRETATION,
    DEFAULT_WEAVIATE_GRPC_PORT,
    DEFAULT_WEAVIATE_HTTP_URL,
)
from src.knowledge.business_interpretation_context import iter_entities_by_types, structure_class_role
from src.knowledge.business_interpretation_runner import run_business_interpretations
from src.knowledge.method_interpretation_runner import _is_trivial_accessor, run_method_interpretations
from src.knowledge.weaviate_business_store import WeaviateBusinessInterpretStore
from src.knowledge.weaviate_interpretation_store import WeaviateMethodInterpretStore
from src.models.structure import EntityType, StructureFacts
from src.persistence.repositories.structure_facts_repository import (
    FileStructureFactsRepository,
    StructureFactsRepository,
)
from src.pipeline.config_bootstrap import config_to_domain, load_config
from src.pipeline.interpretation_policy import InterpretationPipelinePolicy

_LOG = logging.getLogger(__name__)


def get_interpretation_progress_from_weaviate(
    config_path: str | Path,
    structure_facts_json: Optional[str | Path] = None,
) -> dict[str, dict[str, int]]:
    """
    从 Weaviate 查询解读进度（done/total），作为真实数据源。
    返回 {InterpretPhase: {"done": N, "total": M}, ...}（键为 tech/biz 字符串）。
    """
    _pt, _pb = InterpretPhase.TECH.value, InterpretPhase.BIZ.value
    result: dict[str, dict[str, int]] = {_pt: {"done": 0, "total": 0}, _pb: {"done": 0, "total": 0}}
    structure_repo = FileStructureFactsRepository()
    try:
        config = load_config(config_path)
        path = structure_repo.resolve_structure_facts_path(
            config_path=config_path, structure_facts_json=structure_facts_json
        )
        if not path.exists():
            return result
        raw = json.loads(path.read_text(encoding="utf-8"))
        structure_facts = StructureFacts.model_validate(raw)
        k = config.knowledge
        _prog_policy = InterpretationPipelinePolicy.from_knowledge_config(k)

        if _prog_policy.tech_batch_runnable(True):
            all_methods = [
                e
                for e in structure_facts.entities
                if e.type == EntityType.METHOD
                and (e.attributes or {}).get("code_snippet")
                and not _is_trivial_accessor(e)
            ]
            store = WeaviateMethodInterpretStore(
                url=k.vectordb_interpret.weaviate_url or DEFAULT_WEAVIATE_HTTP_URL,
                grpc_port=k.vectordb_interpret.weaviate_grpc_port or DEFAULT_WEAVIATE_GRPC_PORT,
                collection_name=k.vectordb_interpret.collection_name or DEFAULT_COLLECTION_METHOD_INTERPRETATION,
                dimension=k.vectordb_interpret.dimension,
                api_key=k.vectordb_interpret.weaviate_api_key,
            )
            try:
                result[_pt] = {"done": store.count(), "total": len(all_methods)}
            finally:
                store.close()

        if _prog_policy.business_batch_runnable(True):
            all_classes = [
                c
                for c in iter_entities_by_types(structure_facts, [EntityType.CLASS, EntityType.INTERFACE])
                if structure_class_role(c) in ("Controller", "Service")
            ]
            all_methods_biz = [
                e for e in structure_facts.entities
                if e.type == EntityType.METHOD and (e.attributes or {}).get("path")
            ]
            all_modules = sorted(
                {e.module_id for e in structure_facts.entities if e.module_id},
                key=lambda x: x or "",
            )
            biz_total = len(all_classes) + len(all_methods_biz) + len(all_modules)
            store = WeaviateBusinessInterpretStore(
                url=k.vectordb_business.weaviate_url or DEFAULT_WEAVIATE_HTTP_URL,
                grpc_port=k.vectordb_business.weaviate_grpc_port or DEFAULT_WEAVIATE_GRPC_PORT,
                collection_name=k.vectordb_business.collection_name or DEFAULT_COLLECTION_BUSINESS_INTERPRETATION,
                dimension=k.vectordb_business.dimension,
                api_key=k.vectordb_business.weaviate_api_key,
            )
            try:
                result[_pb] = {"done": store.count(), "total": biz_total}
            finally:
                store.close()
    except Exception as e:
        if isinstance(e, (OSError, json.JSONDecodeError)):
            _LOG.warning(
                "解读进度查询失败（已返回零进度）: %s: %s",
                type(e).__name__,
                e,
            )
        else:
            _LOG.error(
                "解读进度查询未预期失败（已返回零进度）: %s: %s",
                type(e).__name__,
                e,
                exc_info=True,
            )
    return result

def structure_facts_cache_path(config_path: str | Path) -> Path:
    """与完整流水线写入位置一致（约定见 ``src.core.paths``）。"""
    return structure_facts_interpret_cache_path_from_config(config_path)

def run_interpretations_only(
    config_path: str | Path,
    *,
    structure_facts_json: Optional[str | Path] = None,
    progress_callback: Optional[Any] = None,
    step_callback: Optional[Any] = None,
    include_method_interpretation: bool = True,
    include_business_interpretation: bool = True,
    item_list_callback: Optional[Any] = None,
    item_list_callback_tech: Optional[Any] = None,
    item_list_callback_biz: Optional[Any] = None,
    item_completed_callback: Optional[Any] = None,
    item_completed_callback_tech: Optional[Any] = None,
    item_completed_callback_biz: Optional[Any] = None,
    item_started_callback_tech: Optional[Callable[[str, InterpretPhase], None]] = None,
    item_started_callback_biz: Optional[Callable[[str, InterpretPhase], None]] = None,
    interpretation_stats_callback: Optional[Callable[[int, int, InterpretPhase], None]] = None,
    structure_facts_repo: StructureFactsRepository | None = None,
    app_context: AppContext | None = None,
) -> dict[str, Any]:
    """
    不重建图谱、不清 Neo4j、不写代码向量：仅基于已缓存的结构事实跑技术解读与/或业务解读。
    适用：代码未变，只需继续或补跑 LLM 解读。

    ``app_context``：刷新配置快照的目标；省略时使用 ``AppContext`` 单例。
    """
    config = load_config(config_path)
    app_ctx = app_context if app_context is not None else AppContext.get()
    app_ctx.set_config(config.model_dump())
    structure_repo = structure_facts_repo or FileStructureFactsRepository()
    path = structure_repo.resolve_structure_facts_path(
        config_path=config_path, structure_facts_json=structure_facts_json
    )
    if not path.exists():
        # 让上层（UI/runner）展示更友好的错误信息
        raise FileNotFoundError(
            f"未找到结构事实缓存: {path}。请先完整运行一次流水线以生成 {structure_facts_interpret_cache_display_path()}，或指定正确的 structure_facts JSON 路径。"
        )

    def _step(msg: str) -> None:
        if step_callback:
            try:
                step_callback(msg)
            except Exception as e:
                _LOG.debug("run_interpretations_only: step_callback 失败（已忽略）: %s: %s", type(e).__name__, e, exc_info=True)

    structure_facts = structure_repo.load(
        config_path=config_path, structure_facts_json=structure_facts_json
    )
    domain = config_to_domain(config)
    k = config.knowledge
    _policy = InterpretationPipelinePolicy.from_knowledge_config(k)

    out: dict[str, Any] = {"stage": "interpretations_only", "structure_facts_source": str(path)}

    _item_tech = item_list_callback_tech or item_list_callback
    _item_biz = item_list_callback_biz or item_list_callback
    _done_tech = item_completed_callback_tech or item_completed_callback
    _done_biz = item_completed_callback_biz or item_completed_callback
    _start_tech = item_started_callback_tech
    _start_biz = item_started_callback_biz
    if _policy.tech_batch_runnable(include_method_interpretation):
        _step("【仅解读】方法技术解读 …")
        out["interpretation"] = run_method_interpretations(
            structure_facts,
            k.method_interpretation,
            k.vectordb_interpret,
            step_callback=_step,
            progress_callback=progress_callback,
            item_list_callback=_item_tech,
            item_completed_callback=_done_tech,
            item_started_callback=_start_tech,
            interpretation_stats_callback=interpretation_stats_callback,
        )
    else:
        out["interpretation"] = {"skipped": True, "reason": "未勾选或未启用 method_interpretation/vectordb-interpret"}

    if _policy.business_batch_runnable(include_business_interpretation):
        _step("【仅解读】业务解读 …")
        out["business_interpretation"] = run_business_interpretations(
            structure_facts,
            domain,
            k.business_interpretation,
            k.vectordb_business,
            step_callback=_step,
            progress_callback=progress_callback,
            item_list_callback=_item_biz,
            item_completed_callback=_done_biz,
            item_started_callback=_start_biz,
            interpretation_stats_callback=interpretation_stats_callback,
        )
    else:
        out["business_interpretation"] = {"skipped": True, "reason": "未勾选或未启用 business_interpretation/vectordb-business"}

    _step("【仅解读】全部结束")
    ir = out.get("interpretation") or {}
    br = out.get("business_interpretation") or {}
    parts = []
    if not ir.get("skipped") and ir.get("written") is not None:
        parts.append(f"技术解读 本轮 {ir.get('written', 0)} 条")
    if not br.get("skipped") and br.get("written") is not None:
        parts.append(f"业务解读 本轮 {br.get('written', 0)} 条")
    out["message"] = "；".join(parts) if parts else "未执行解读步骤"
    return out
