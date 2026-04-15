"""批量生成方法技术解读，写入 Weaviate interpretation collection。支持 Ollama、OpenAI、Anthropic 等 LLM 提供者。"""
from __future__ import annotations

import json
import logging
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Optional

from src.models.structure import EntityType, RelationType, StructureFacts, StructureEntity, StructureRelation
from src.core.domain_enums import InterpretPhase
from src.core.weaviate_defaults import (
    DEFAULT_COLLECTION_METHOD_INTERPRETATION,
    DEFAULT_WEAVIATE_GRPC_PORT,
    DEFAULT_WEAVIATE_HTTP_URL,
)
from src.knowledge.weaviate_interpretation_store import WeaviateMethodInterpretStore
from src.knowledge.interpretation_store_adapter import MethodInterpretationStoreAdapter
from src.knowledge.llm import LLMProviderFactory
from src.knowledge.base_interpretation_runner import BaseInterpretationRunner
from src.knowledge.interpretation_item_helpers import interpret_one_llm_embed_store
from src.knowledge.interpretation_runner_inputs import (
    MethodInterpretInput,
    VectorDbInterpretInput,
    coerce_method_interpretation_config,
    coerce_vectordb_config,
)

_LOG = logging.getLogger(__name__)


def _entity_name_by_id(entities: list[StructureEntity], eid: str) -> str:
    for e in entities:
        if e.id == eid:
            return e.name or eid
    return eid


def _build_method_context(
    method: StructureEntity,
    facts: StructureFacts,
    max_callers: int = 5,
    max_callees: int = 8,
) -> tuple[str, str, list[str]]:
    """返回 (class_entity_id, context_summary, related_ids)。"""
    class_id = ""
    for r in facts.relations:
        if r.type == RelationType.CONTAINS and r.target_id == method.id:
            src = r.source_id
            for e in facts.entities:
                if e.id == src and e.type in (EntityType.CLASS, EntityType.INTERFACE):
                    class_id = src
                    break
            if class_id:
                break
    attrs = method.attributes or {}
    class_name = attrs.get("class_name") or ""
    sig = attrs.get("signature") or ""
    callers: list[str] = []
    callees: list[str] = []
    for r in facts.relations:
        if r.type == RelationType.CALLS and r.target_id == method.id:
            callers.append(_entity_name_by_id(facts.entities, r.source_id))
        if r.type == RelationType.CALLS and r.source_id == method.id:
            callees.append(_entity_name_by_id(facts.entities, r.target_id))
    callers = callers[:max_callers]
    callees = callees[:max_callees]
    lines = [
        f"所属类 ID: {class_id or '未知'}",
        f"类名: {class_name or '未知'}",
        f"方法签名: {sig or method.name}",
        f"模块: {method.module_id or ''}",
        f"直接调用本方法的上游方法（节选）: {', '.join(callers) if callers else '无'}",
        f"本方法直接调用的下游方法（节选）: {', '.join(callees) if callees else '无'}",
    ]
    rid_set = {method.id}
    if class_id:
        rid_set.add(class_id)
    for r in facts.relations:
        if r.type == RelationType.CALLS and r.source_id == method.id:
            rid_set.add(r.target_id)
        if r.type == RelationType.CALLS and r.target_id == method.id:
            rid_set.add(r.source_id)
    related_ids = list(rid_set)[:24]
    return class_id, "\n".join(lines), related_ids


def _build_prompt(language: str, context_summary: str, signature: str, code_snippet: str) -> str:
    if (language or "zh").lower().startswith("en"):
        return f"""You are a senior Java engineer. Based on the following CLASS/CALL-CHAIN CONTEXT and METHOD CODE, produce a two-part interpretation.

Requirements:
- Part 1 [Summary]: Keyword-dense, max 50 characters, space-separated key phrases. Include: business actions, involved objects, key technical approaches. No full sentences.
- Part 2 [Detail]: Full technical interpretation covering responsibility, key logic, and call-graph relationships. Do not dump the raw code again.

### Context
{context_summary}

### Signature
{signature}

### Method body (excerpt)
```
{code_snippet[:10000]}
```

### Output (strict format)
[Summary] <keyword1 keyword2 keyword3 ...>

[Detail]
<full technical interpretation>"""

    return f"""你是一名资深 Java 工程师。请根据下面的「类与调用链上下文」以及「方法代码」，输出该方法的技术解读。

要求：
- 使用简体中文，分两部分输出。
- 第一部分 [摘要]：关键词密集，不超过50个中文字符，用空格分隔关键词/短语，不要完整句子。包含：业务动作、涉及对象、关键技术手段。
- 第二部分 [详情]：完整技术解读，说明方法职责、关键逻辑、与上下游调用的关系；不要大段重复粘贴源码。

### 上下文
{context_summary}

### 方法签名
{signature}

### 方法体（节选）
```
{code_snippet[:10000]}
```

### 请输出（严格按以下格式）
[摘要] <关键词1 关键词2 关键词3 ...>

[详情]
<完整技术解读>"""


def _is_trivial_accessor(method: StructureEntity) -> bool:
    """
    识别简单 getter/setter：主要针对数据模型中的 getX/setX/isX，不做技术/业务解读。
    优先使用结构层写入的 AST 标记 is_getter/is_setter；缺失时再退回名称+签名启发式：
    - 名称以 get*/is* 且签名为 name() 视为 getter
    - 名称以 set* 且签名为 name(T)（单参数）视为 setter
    """
    attrs = method.attributes or {}
    if attrs.get("is_getter") or attrs.get("is_setter"):
        return True
    name = (method.name or "").strip()
    sig = ((method.attributes or {}).get("signature") or name).strip()
    if "(" not in sig or ")" not in sig:
        return False
    inside = sig[sig.find("(") + 1 : sig.rfind(")")]
    params = [p for p in (inside.split(",") if inside else []) if p.strip()]
    if (name.startswith("get") or name.startswith("is")) and not params:
        return True
    if name.startswith("set") and len(params) == 1:
        return True
    return False


def run_method_interpretations(
    structure_facts: StructureFacts,
    interpret_cfg: MethodInterpretInput,
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
    对每个含 code_snippet 的方法调用 LLM，写入 Weaviate。
    单条失败则跳过，不阻塞全库。

    ``interpret_cfg`` / ``vectordb_cfg`` 推荐使用 ``MethodInterpretationConfig`` /
    ``VectorDBConfig``；仍兼容 plain dict（经 Pydantic 校验）。
    """
    mi = coerce_method_interpretation_config(interpret_cfg)
    vinterp = coerce_vectordb_config(vectordb_cfg)

    runner = BaseInterpretationRunner(
        step_callback=step_callback,
        progress_callback=progress_callback,
        item_completed_callback=item_completed_callback,
        item_started_callback=item_started_callback,
        item_list_callback=item_list_callback,
    )

    if not mi.enabled:
        return {"skipped": True, "written": 0, "failed": 0}

    if vinterp.backend != "weaviate" or not vinterp.enabled:
        runner.step("技术解读：未启用 vectordb-interpret，已跳过")
        return {"skipped": True, "written": 0, "failed": 0}

    lang = (mi.language or "zh").lower()
    max_m = int(mi.max_methods or 0)
    backend = (mi.llm_backend or "ollama").strip()
    llm_sel = LLMProviderFactory.from_method_interpretation(mi)
    llm = llm_sel.provider
    backend_display = llm_sel.resolved_backend
    runner.step(f"技术解读：请求后端 {backend}，实际使用 {llm_sel.resolved_backend}")

    # 全部候选方法（非 getter/setter，且有 code_snippet）
    all_methods = [
        e
        for e in structure_facts.entities
        if e.type == EntityType.METHOD
        and (e.attributes or {}).get("code_snippet")
        and not _is_trivial_accessor(e)
    ]

    # 与历史 ``dict.get('dimension') or 64`` 对齐：模型默认维度为 1024 时仍直接使用
    dim = int(vinterp.dimension) if vinterp.dimension else 64
    store: Optional[MethodInterpretationStoreAdapter] = None
    ok, fail = 0, 0

    try:
        weaviate_store = WeaviateMethodInterpretStore(
            url=vinterp.weaviate_url or DEFAULT_WEAVIATE_HTTP_URL,
            grpc_port=int(vinterp.weaviate_grpc_port or DEFAULT_WEAVIATE_GRPC_PORT),
            collection_name=vinterp.collection_name or DEFAULT_COLLECTION_METHOD_INTERPRETATION,
            dimension=dim,
            api_key=vinterp.weaviate_api_key,
        )
        store = MethodInterpretationStoreAdapter(weaviate_store)

        # ── 一致性同步：以 structure_facts 为真相源，清理 Weaviate 中的孤儿 ──
        valid_method_ids = {e.id for e in all_methods}
        existing_ids = store.list_existing_keys()
        orphan_ids = existing_ids - valid_method_ids
        if orphan_ids:
            runner.step(f"一致性同步：发现 {len(orphan_ids)} 条孤儿解读（方法已不存在），正在清理…")
            for oid in orphan_ids:
                try:
                    uid = weaviate_store._to_uuid(oid + "|interpret")
                    weaviate_store._get_collection().data.delete_by_id(uid)
                except Exception:
                    pass  # 删除失败不影响主流程
            existing_ids -= orphan_ids
            runner.step(f"一致性同步：已清理 {len(orphan_ids)} 条孤儿")

        # 已有解读的方法 ID，用于断点续跑时跳过
        total_candidates = len(all_methods)
        already_done = sum(1 for m in all_methods if m.id in existing_ids)

        # 本轮需要新解读的方法列表（按 max_methods 截断）
        todo_methods = [m for m in all_methods if m.id not in existing_ids]
        if max_m > 0:
            todo_methods = todo_methods[:max_m]

        if runner.step_callback:
            runner.step(
                f"技术解读：候选方法 {total_candidates} 条，其中已存在解读 {already_done} 条，"
                f"本轮计划新解读 {len(todo_methods)} 条（LLM: {backend_display}）"
            )
            if llm_sel.fallback_reason:
                runner.step(
                    f"技术解读：请求后端 {backend}，实际使用 {llm_sel.resolved_backend}，原因：{llm_sel.fallback_reason}"
                )
        # 诊断：若应有已有解读但未匹配到，提示用户检查
        if already_done == 0 and total_candidates > 0 and runner.step_callback:
            weaviate_count = store.count()
            if weaviate_count > 0 and not existing_ids:
                runner.step("提示：Weaviate 中有解读记录但未能读取 method_id，将全量处理。请检查 vectordb-interpret 连接。")
            elif weaviate_count > 0 and existing_ids:
                runner.step(
                    "提示：Weaviate 中有解读记录但 entity_id 与当前结构事实不匹配，将全量处理。"
                    "请确认结构事实 JSON 与首次解读时来自同一项目。"
                )

        if interpretation_stats_callback:
            try:
                # 使用 Weaviate Collection 中的对象总数作为真实解读进度（不依赖 structure_facts 的 ID 匹配）
                weaviate_count = store.count()
                interpretation_stats_callback(weaviate_count, total_candidates, InterpretPhase.TECH)
            except Exception:
                pass

        def _method_display_label(m) -> str:
            """方法显示标签：签名（类名），与 Step 3 列表格式一致。"""
            attrs = m.attributes or {}
            sig = attrs.get("signature") or m.name
            cls = attrs.get("class_name") or ""
            return f"{sig}（{cls}）" if cls else sig

        # 通知 UI 全部候选方法清单（含已解读+待解读），便于滑动窗口显示当前进度附近的方法
        # 传入 (label, done) 元组，已存在解读的预标记为 done=True
        if all_methods:
            items_with_done = [
                (_method_display_label(m), m.id in existing_ids)
                for m in all_methods
            ]
            runner.publish_item_list(items_with_done)

        def _run_items(items_seq: list[StructureEntity]) -> None:
            nonlocal ok, fail
            total = len(items_seq) or 1
            timeout_sec = int(mi.timeout_seconds or 120)
            max_workers = max(1, int(getattr(mi, "max_workers", 4) or 4))
            counter_lock = threading.Lock()
            processed_count = 0

            # 进度预估
            if runner.step_callback and total > 1:
                est_min = total * 25 / max_workers / 60
                runner.step(
                    f"技术解读开始：{total} 个方法，并发 {max_workers} 路，预计 {est_min:.0f} 分钟"
                )

            def _process_one(method: StructureEntity) -> tuple[int, int]:
                """单个方法的解读处理（线程安全）。"""
                snippet = (method.attributes or {}).get("code_snippet") or ""
                class_id, ctx, related_ids = _build_method_context(method, structure_facts)
                sig = (method.attributes or {}).get("signature") or method.name
                display_label = _method_display_label(method)
                prompt = _build_prompt(lang, ctx, sig, snippet)

                def _persist(
                    text: str,
                    vec: list[float],
                    m=method,
                    cid=class_id,
                    cctx=ctx,
                    s=sig,
                    rids=related_ids,
                ) -> tuple[bool, bool]:
                    return weaviate_store.add_with_created(
                        vec,
                        method_entity_id=m.id,
                        interpretation_text=text,
                        class_entity_id=cid,
                        class_name=(m.attributes or {}).get("class_name") or "",
                        method_name=m.name or "",
                        signature=s,
                        context_summary=cctx[:4000],
                        language="en" if lang.startswith("en") else "zh",
                        related_entity_ids_json=json.dumps(rids, ensure_ascii=False),
                    )

                return interpret_one_llm_embed_store(
                    runner,
                    display_label,
                    InterpretPhase.TECH,
                    llm=llm,
                    prompt=prompt,
                    timeout=timeout_sec,
                    min_text_len=10,
                    embedding_dim=dim,
                    persist=_persist,
                )

            # 分批并行处理，每批 batch_size 个方法
            batch_size = max(max_workers * 2, 20)
            for batch_start in range(0, total, batch_size):
                batch = items_seq[batch_start : batch_start + batch_size]
                with ThreadPoolExecutor(max_workers=max_workers) as pool:
                    futures = {pool.submit(_process_one, m): m for m in batch}
                    for future in as_completed(futures):
                        try:
                            o, f = future.result()
                        except Exception:
                            _LOG.exception("技术解读：并发任务异常（已计入失败）")
                            o, f = 0, 1
                        with counter_lock:
                            ok += o
                            fail += f
                            processed_count += 1
                        if runner.progress_callback:
                            pct = 85 + int(15 * processed_count / total)
                            runner.progress(min(pct, 99), 100, f"技术解读 {processed_count}/{total}…")

        _run_items(todo_methods)

        if runner.step_callback:
            runner.step(
                f"技术解读完成：本轮成功 {ok}，失败 {fail}；"
                f"累计已有解读 {already_done + ok} / 候选 {total_candidates}"
                f"，已写入 Weaviate「{vinterp.collection_name or DEFAULT_COLLECTION_METHOD_INTERPRETATION}」"
            )
        if runner.progress_callback:
            runner.progress(100, 100, "流水线全部完成")

    finally:
        if store is not None:
            try:
                store.close()
            except OSError as e:
                _LOG.warning("技术解读：关闭向量存储失败（已忽略）: %s", e)
            except Exception:
                _LOG.exception("技术解读：关闭向量存储出现未预期错误（已忽略）")

    return {
        "written": ok,
        "failed": fail,
        "total_candidates": total_candidates,
        "already_done_before": already_done,
        "todo_this_run": len(todo_methods),
    }
