"""整体业务解读：基于结构/语义事实与领域配置，调用 LLM 生成三层业务说明。支持 Ollama、OpenAI、Anthropic 等。"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional

from src.models import StructureFacts, EntityType, DomainKnowledge
from src.core.domain_enums import BusinessInterpretLevel, InterpretPhase
from src.core.weaviate_defaults import (
    DEFAULT_COLLECTION_BUSINESS_INTERPRETATION,
    DEFAULT_WEAVIATE_GRPC_PORT,
    DEFAULT_WEAVIATE_HTTP_URL,
)
from src.knowledge.weaviate_business_store import WeaviateBusinessInterpretStore
from src.knowledge.interpretation_store_adapter import BusinessInterpretationStoreAdapter
from src.knowledge.llm import LLMProviderFactory
from src.knowledge.base_interpretation_runner import BaseInterpretationRunner
from src.knowledge.business_interpretation_strategies import BusinessInterpretTierSpec
from src.knowledge.interpretation_item_helpers import interpret_one_llm_embed_store
from src.knowledge.business_interpretation_context import (
    build_api_context,
    build_api_prompt,
    build_class_context,
    build_class_prompt,
    build_module_context,
    build_module_prompt,
    format_domain_background,
    iter_entities_by_types,
    structure_class_role,
)
from src.knowledge.interpretation_runner_inputs import (
    BusinessInterpretInput,
    VectorDbInterpretInput,
    coerce_business_interpretation_config,
    coerce_vectordb_config,
)

_LOG = logging.getLogger(__name__)


def run_business_interpretations(
    structure_facts: StructureFacts,
    domain: DomainKnowledge,
    biz_cfg: BusinessInterpretInput,
    vectordb_cfg: VectorDbInterpretInput,
    *,
    step_callback: Optional[Callable[[str], None]] = None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    item_list_callback: Optional[Callable[[list[str]], None]] = None,
    item_completed_callback: Optional[Callable[[str, bool], None]] = None,
    item_started_callback: Optional[Callable[[str, InterpretPhase], None]] = None,
    interpretation_stats_callback: Optional[Callable[[int, int, InterpretPhase], None]] = None,
) -> dict[str, Any]:
    """
    三层业务解读：
    - class/service 级
    - API/use-case 级
    - module/service 级综述

    ``biz_cfg`` / ``vectordb_cfg`` 推荐使用 ``BusinessInterpretationConfig`` /
    ``VectorDBConfig``；仍兼容 plain dict。
    """
    bi = coerce_business_interpretation_config(biz_cfg)
    vbiz = coerce_vectordb_config(vectordb_cfg)

    runner = BaseInterpretationRunner(
        step_callback=step_callback,
        progress_callback=progress_callback,
        item_completed_callback=item_completed_callback,
        item_started_callback=item_started_callback,
        item_list_callback=item_list_callback,
    )

    if not bi.enabled:
        return {"skipped": True, "written": 0, "failed": 0}
    if vbiz.backend != "weaviate" or not vbiz.enabled:
        runner.step("业务解读：未启用 vectordb-business，已跳过")
        return {"skipped": True, "written": 0, "failed": 0}

    lang = (bi.language or "zh").lower()
    max_classes = int(bi.max_classes or 0)
    backend = (bi.llm_backend or "ollama").strip()
    llm_sel = LLMProviderFactory.from_business_interpretation(bi)
    llm = llm_sel.provider
    backend_display = llm_sel.resolved_backend
    runner.step(f"业务解读：请求后端 {backend}，实际使用 {llm_sel.resolved_backend}")
    timeout = int(bi.timeout_seconds or 180)
    max_apis = int(bi.max_apis or 0)
    max_modules = int(bi.max_modules or 0)

    all_classes = [
        c
        for c in iter_entities_by_types(structure_facts, [EntityType.CLASS, EntityType.INTERFACE])
        if structure_class_role(c) in ("Controller", "Service")
    ]
    all_methods = [
        e for e in structure_facts.entities if e.type == EntityType.METHOD and (e.attributes or {}).get("path")
    ]
    all_modules: list[str] = sorted(
        {e.module_id for e in structure_facts.entities if e.module_id}, key=lambda x: x or ""
    )

    dim = int(vbiz.dimension) if vbiz.dimension else 1024
    store: Optional[BusinessInterpretationStoreAdapter] = None
    ok, fail = 0, 0
    processed = 0
    domain_text = format_domain_background(domain)

    try:
        weaviate_store = WeaviateBusinessInterpretStore(
            url=vbiz.weaviate_url or DEFAULT_WEAVIATE_HTTP_URL,
            grpc_port=int(vbiz.weaviate_grpc_port or DEFAULT_WEAVIATE_GRPC_PORT),
            collection_name=vbiz.collection_name or DEFAULT_COLLECTION_BUSINESS_INTERPRETATION,
            dimension=dim,
            api_key=vbiz.weaviate_api_key,
        )
        store = BusinessInterpretationStoreAdapter(weaviate_store)
        existing = store.list_existing_keys()

        todo_classes = [c for c in all_classes if (c.id, BusinessInterpretLevel.CLASS.value) not in existing]
        done_class = len(all_classes) - len(todo_classes)
        if max_classes > 0:
            todo_classes = todo_classes[:max_classes]
        classes = todo_classes

        todo_methods = [m for m in all_methods if (m.id, BusinessInterpretLevel.API.value) not in existing]
        done_api = len(all_methods) - len(todo_methods)
        if max_apis > 0:
            todo_methods = todo_methods[:max_apis]
        methods = todo_methods

        todo_mods = [mid for mid in all_modules if (mid, BusinessInterpretLevel.MODULE.value) not in existing]
        done_mod = len(all_modules) - len(todo_mods)
        if max_modules > 0:
            todo_mods = todo_mods[:max_modules]
        modules = todo_mods

        total_targets = len(classes) + len(methods) + len(modules)
        if runner.step_callback:
            runner.step(
                f"业务解读（增量）：类 候选 {len(all_classes)} 已有 {done_class} 本轮 {len(classes)}；"
                f"API 候选 {len(all_methods)} 已有 {done_api} 本轮 {len(methods)}；"
                f"模块 候选 {len(all_modules)} 已有 {done_mod} 本轮 {len(modules)}（LLM: {backend_display}）"
            )
            if llm_sel.fallback_reason:
                runner.step(
                    f"业务解读：请求后端 {backend}，实际使用 {llm_sel.resolved_backend}，原因：{llm_sel.fallback_reason}"
                )

        biz_total = len(all_classes) + len(all_methods) + len(all_modules)
        if interpretation_stats_callback and biz_total > 0:
            try:
                # 使用 Weaviate Collection 中的对象总数作为真实解读进度
                weaviate_count = store.count()
                interpretation_stats_callback(weaviate_count, biz_total, InterpretPhase.BIZ)
            except Exception:
                pass

        def _api_display_label(m) -> str:
            """API 方法显示标签：签名（类名），与 Step 3 列表格式一致。"""
            attrs = m.attributes or {}
            sig = attrs.get("signature") or m.name
            cls = attrs.get("class_name") or ""
            return f"{sig}（{cls}）" if cls else sig

        # 通知 UI 本轮待处理的业务解读清单（类、API、模块）
        if total_targets:
            items: list[str] = []
            for c in classes:
                items.append(f"类: {c.name or c.id}")
            for m in methods:
                items.append(_api_display_label(m))
            for mid in modules:
                items.append(f"模块: {mid}")
            runner.publish_item_list(items)

        def _run_items(
            *,
            items_seq: list[Any],
            label_fn: Callable[[Any], str],
            prompt_fn: Callable[[Any], str],
            add_kwargs_fn: Callable[[Any, str], dict[str, Any]],
            min_len: int,
            pct_cap: int,
            msg_prefix: str,
        ) -> int:
            """统一循环：LLM -> embedding -> store.add，并通过 runner 回调更新 UI。返回成功数。"""
            nonlocal ok, fail, processed
            if not items_seq:
                return 0
            local_ok = 0
            for i, it in enumerate(items_seq):
                label = label_fn(it)
                prompt = prompt_fn(it)

                def _persist(text: str, vec: list[float], item=it) -> tuple[bool, bool]:
                    return weaviate_store.add_with_created(vec, **add_kwargs_fn(item, text))

                o, f = interpret_one_llm_embed_store(
                    runner,
                    label,
                    InterpretPhase.BIZ,
                    llm=llm,
                    prompt=prompt,
                    timeout=timeout,
                    min_text_len=min_len,
                    embedding_dim=dim,
                    persist=_persist,
                )
                ok += o
                fail += f
                local_ok += o
                processed += 1
                if progress_callback and total_targets:
                    pct = 78 + int(pct_cap * processed / total_targets)
                    runner.progress(min(pct, 99), 100, f"{msg_prefix} {i + 1}/{len(items_seq)}…")
            return local_ok

        # 1. 类/Service 级
        def _class_label(c) -> str:
            return f"类: {c.name or c.id}"

        def _class_prompt(c) -> str:
            _biz_domain, _caps, _ctx, _role, _module_id = build_class_context(c, structure_facts, domain)
            return build_class_prompt(lang, domain_text, _ctx)

        def _class_add_kwargs(c, text: str) -> dict[str, Any]:
            _biz_domain, _caps, _ctx, _role, _module_id = build_class_context(c, structure_facts, domain)
            return dict(
                entity_id=c.id,
                level=BusinessInterpretLevel.CLASS.value,
                summary_text=text,
                entity_type=c.type.value,
                business_domain=_biz_domain,
                business_capabilities=_caps,
                language="en" if lang.startswith("en") else "zh",
                context_json=json.dumps({"role": _role, "module_id": _module_id, "context": _ctx}, ensure_ascii=False),
                related_entity_ids_json=json.dumps([c.id], ensure_ascii=False),
            )

        # 2. API/use-case 级（prompt / kwargs 构造）
        def _api_prompt(m) -> str:
            _biz_domain, _uc, _ctx, _related = build_api_context(m, structure_facts, domain)
            return build_api_prompt(lang, domain_text, _ctx)

        def _api_add_kwargs(m, text: str) -> dict[str, Any]:
            _biz_domain, _uc, _ctx, _related = build_api_context(m, structure_facts, domain)
            return dict(
                entity_id=m.id,
                level=BusinessInterpretLevel.API.value,
                summary_text=text,
                entity_type=m.type.value,
                business_domain=_biz_domain,
                business_capabilities=_uc,
                language="en" if lang.startswith("en") else "zh",
                context_json=json.dumps({"use_case": _uc, "context": _ctx}, ensure_ascii=False),
                related_entity_ids_json=json.dumps(_related, ensure_ascii=False),
            )

        # 3. 模块级
        def _mod_label(mid: str) -> str:
            return f"模块: {mid}"

        def _mod_prompt(mid: str) -> str:
            _biz_domain, _caps, _ctx, _related = build_module_context(mid, structure_facts, domain)
            return build_module_prompt(lang, domain_text, _ctx)

        def _mod_add_kwargs(mid: str, text: str) -> dict[str, Any]:
            _biz_domain, _caps, _ctx, _related = build_module_context(mid, structure_facts, domain)
            return dict(
                entity_id=mid,
                level=BusinessInterpretLevel.MODULE.value,
                summary_text=text,
                entity_type=BusinessInterpretLevel.MODULE.value,
                business_domain=_biz_domain,
                business_capabilities=_caps,
                language="en" if lang.startswith("en") else "zh",
                context_json=json.dumps({"context": _ctx}, ensure_ascii=False),
                related_entity_ids_json=json.dumps(_related, ensure_ascii=False),
            )

        tiers: list[BusinessInterpretTierSpec] = [
            BusinessInterpretTierSpec(
                items=classes,
                msg_prefix="业务解读：类",
                min_text_len=20,
                pct_cap=10,
                label_fn=_class_label,
                prompt_fn=_class_prompt,
                add_kwargs_fn=_class_add_kwargs,
            ),
            BusinessInterpretTierSpec(
                items=methods,
                msg_prefix="业务解读：API",
                min_text_len=20,
                pct_cap=10,
                label_fn=_api_display_label,
                prompt_fn=_api_prompt,
                add_kwargs_fn=_api_add_kwargs,
            ),
            BusinessInterpretTierSpec(
                items=modules,
                msg_prefix="业务解读：模块",
                min_text_len=20,
                pct_cap=10,
                label_fn=_mod_label,
                prompt_fn=_mod_prompt,
                add_kwargs_fn=_mod_add_kwargs,
            ),
        ]

        ok_c = ok_a = ok_m = 0
        for idx, tier in enumerate(tiers):
            n = _run_items(
                items_seq=list(tier.items),
                label_fn=tier.label_fn,
                prompt_fn=tier.prompt_fn,
                add_kwargs_fn=tier.add_kwargs_fn,
                min_len=tier.min_text_len,
                pct_cap=tier.pct_cap,
                msg_prefix=tier.msg_prefix,
            )
            if idx == 0:
                ok_c = n
            elif idx == 1:
                ok_a = n
            else:
                ok_m = n

        if runner.step_callback:
            runner.step(
                f"业务解读完成：本轮成功 {ok}，失败 {fail}；"
                f"累计约 类 {done_class + ok_c}/{len(all_classes)}，"
                f"API {done_api + ok_a}/{len(all_methods)}，"
                f"模块 {done_mod + ok_m}/{len(all_modules)}"
                f"，已写入「{vbiz.collection_name or DEFAULT_COLLECTION_BUSINESS_INTERPRETATION}」"
            )
        if runner.progress_callback:
            runner.progress(100, 100, "业务解读阶段完成")
    finally:
        if store is not None:
            try:
                store.close()
            except OSError as e:
                _LOG.warning("业务解读：关闭向量存储失败（已忽略）: %s", e)
            except Exception:
                _LOG.exception("业务解读：关闭向量存储出现未预期错误（已忽略）")

    return {
        "written": ok,
        "failed": fail,
        "total_targets": total_targets,
        "todo_this_run_class": len(classes),
        "todo_this_run_api": len(methods),
        "todo_this_run_module": len(modules),
        "candidates_class": len(all_classes),
        "candidates_api": len(all_methods),
        "candidates_module": len(all_modules),
        "already_class_before": done_class,
        "already_api_before": done_api,
        "already_module_before": done_mod,
    }

