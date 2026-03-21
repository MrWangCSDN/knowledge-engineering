"""基于 structure_facts 的设计模式与架构模式识别（LLM + 结构证据上下文）。"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from src.knowledge.pattern_recognition_catalog import allowed_pattern_names, format_allowed_patterns_for_prompt
from src.knowledge.pattern_recognition_context_builders import (
    build_module_pattern_context,
    build_system_pattern_context,
)
from src.knowledge.weaviate_pattern_store import WeaviatePatternInterpretStore
from src.semantic.embedding import get_embedding
from src.models.structure import StructureFacts

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Evidence:
    entity_ids: list[str]
    notes: str


@dataclass(frozen=True)
class RecognizedPattern:
    pattern_type: str  # design | architecture
    pattern_name: str
    confidence: float
    summary: str
    evidence: Evidence


def _extract_json(text: str) -> Any:
    """尽量从 LLM 输出中提取 JSON（支持 ```json 块）。"""
    if not text:
        return None
    # 去掉 markdown fences
    cleaned = re.sub(r"^```(?:json)?\s*|```$", "", text.strip(), flags=re.IGNORECASE | re.MULTILINE)
    # 尝试直接解析
    try:
        return json.loads(cleaned)
    except Exception:
        pass

    # 兜底：找首个 { 或 [ 到最后匹配
    m = re.search(r"[\\[{]", cleaned)
    if not m:
        return None
    start = m.start()
    candidate = cleaned[start:]
    try:
        return json.loads(candidate)
    except Exception:
        return None


def _clamp_confidence(v: Any) -> float:
    try:
        f = float(v)
    except Exception:
        return 0.0
    if f < 0.0:
        return 0.0
    if f > 1.0:
        return 1.0
    return f


def _validate_and_normalize_patterns(
    raw: Any,
    *,
    allowed_design: set[str],
    allowed_arch: set[str],
) -> list[RecognizedPattern]:
    if not isinstance(raw, dict):
        return []
    items = raw.get("top_patterns") or raw.get("patterns") or []
    if not isinstance(items, list):
        return []

    out: list[RecognizedPattern] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        ptype = (it.get("pattern_type") or "").strip().lower()
        pname = (it.get("pattern_name") or "").strip()
        conf = _clamp_confidence(it.get("confidence", 0.0))
        summary = (it.get("summary") or it.get("summary_text") or it.get("description") or "").strip()
        evidence = it.get("evidence") or {}
        if isinstance(evidence, dict):
            entity_ids = evidence.get("entity_ids") or evidence.get("entities") or []
            if isinstance(entity_ids, str):
                entity_ids = [entity_ids]
            if not isinstance(entity_ids, list):
                entity_ids = []
            entity_ids = [str(x) for x in entity_ids if x]
            notes = (evidence.get("notes") or evidence.get("reason") or "").strip()
        else:
            entity_ids = []
            notes = str(evidence)

        if ptype == "design":
            if pname not in allowed_design:
                continue
        elif ptype == "architecture":
            if pname not in allowed_arch:
                continue
        else:
            continue

        # summary 太短通常表示模型没理解任务；最低要求是非空
        if not summary:
            continue

        out.append(
            RecognizedPattern(
                pattern_type=ptype,
                pattern_name=pname,
                confidence=conf,
                summary=summary[:5000],
                evidence=Evidence(entity_ids=entity_ids[:32], notes=notes[:2000]),
            )
        )
    return out


def _build_prompt(
    *,
    language: str,
    scope_type: str,
    target_id: str,
    top_n: int,
    context: str,
) -> str:
    allowed_prompt = format_allowed_patterns_for_prompt()
    lang_hint = "简体中文" if (language or "zh").lower().startswith("zh") else "English"
    return f"""你是资深软件架构师与重构顾问。

请基于给定的 code 结构证据（来自 structure_facts）识别最可能的【设计模式】与【架构模式】。

{allowed_prompt}

识别任务：
- scope_type = "{scope_type}"，target_id = "{target_id}"
- 只输出最可能的 Top {top_n} 个模式；按置信度从高到低排序
- pattern_name 必须严格从允许列表中选择（不允许杜撰/近似名称）
- evidence 需要尽量给出与你结论直接相关的 entity_id（来自 structure_facts），并简述为什么

输出要求：
- 只输出严格 JSON，不要输出 Markdown、不要输出额外解释
- JSON 结构如下：
{{
  "scope_type": "{scope_type}",
  "target_id": "{target_id}",
  "top_patterns": [
    {{
      "pattern_type": "design" | "architecture",
      "pattern_name": "允许列表中的名称",
      "confidence": 0.0-1.0,
      "summary": "用 {lang_hint} 给出该模式在本 scope 下的作用/体现方式（尽量具体但不贴大量源码）",
      "evidence": {{
        "entity_ids": ["..."],
        "notes": "简要说明证据与推断逻辑"
      }}
    }}
  ]
}}

下面是结构证据（不要原样复述它，只用来推断）：
{context}
"""


def _heuristic_fallback(
    *,
    facts: StructureFacts,
    scope_type: str,
    target_id: str,
    top_n: int,
    language: str,
) -> list[RecognizedPattern]:
    """LLM 失败/解析失败时的兜底：用名字关键词做粗略候选（低置信度）。"""
    # 只做非常轻量的候选：避免“看起来很像但实际上不对”的高置信度
    name_tokens = []
    for e in facts.entities:
        if scope_type == "module" and (e.module_id != target_id):
            continue
        n = (e.name or "").strip()
        if n:
            name_tokens.append(n)
        if e.type.value == "method":
            attrs = e.attributes or {}
            sig = attrs.get("signature") or e.name or ""
            if isinstance(sig, str) and sig:
                name_tokens.append(sig)

    blob = " ".join(name_tokens).lower()
    allowed_design, allowed_arch = allowed_pattern_names()
    allowed_design_set = set(allowed_design)
    allowed_arch_set = set(allowed_arch)

    def add_if(name: str, ptype: str, conf: float) -> Optional[RecognizedPattern]:
        if ptype == "design" and name not in allowed_design_set:
            return None
        if ptype == "architecture" and name not in allowed_arch_set:
            return None
        if conf <= 0:
            return None
        summary = f"{language}-heuristic: 基于命名关键词的弱信号候选（置信度偏低）。模式：{name}"
        return RecognizedPattern(
            pattern_type=ptype,
            pattern_name=name,
            confidence=conf,
            summary=summary[:5000],
            evidence=Evidence(entity_ids=[], notes="LLM 输出解析失败时的兜底候选（仅用于可视化/快速提示）。"),
        )

    out: list[RecognizedPattern] = []
    # Design patterns
    if "singleton" in blob or "getinstance" in blob or "get_instance" in blob:
        p = add_if("Singleton", "design", 0.35)
        if p:
            out.append(p)
    if "factory" in blob or "newfactory" in blob or "create" in blob:
        p = add_if("Factory Method", "design", 0.3)
        if p:
            out.append(p)
    if "abstractfactory" in blob:
        p = add_if("Abstract Factory", "design", 0.25)
        if p:
            out.append(p)
    if "builder" in blob:
        p = add_if("Builder", "design", 0.28)
        if p:
            out.append(p)
    if "adapter" in blob or "convert" in blob or "translate" in blob:
        p = add_if("Adapter", "design", 0.26)
        if p:
            out.append(p)
    if "decorator" in blob or "wrap" in blob:
        p = add_if("Decorator", "design", 0.24)
        if p:
            out.append(p)
    if "facade" in blob:
        p = add_if("Facade", "design", 0.26)
        if p:
            out.append(p)
    if "proxy" in blob:
        p = add_if("Proxy", "design", 0.24)
        if p:
            out.append(p)
    if "observer" in blob or "listener" in blob or "event" in blob:
        p = add_if("Observer", "design", 0.22)
        if p:
            out.append(p)
    if "strategy" in blob:
        p = add_if("Strategy", "design", 0.22)
        if p:
            out.append(p)
    if "template" in blob:
        p = add_if("Template Method", "design", 0.2)
        if p:
            out.append(p)
    if "iterator" in blob:
        p = add_if("Iterator", "design", 0.2)
        if p:
            out.append(p)

    # Architecture
    # 注意：这里是弱信号兜底，不追求精确
    if "controller" in blob and "service" in blob:
        p = add_if("MVC (Model-View-Controller)", "architecture", 0.35)
        if p:
            out.append(p)
        p = add_if("Layered Architecture", "architecture", 0.32)
        if p:
            out.append(p)
    if "event" in blob or "listener" in blob or "subscriber" in blob:
        p = add_if("Event-Driven Architecture", "architecture", 0.28)
        if p:
            out.append(p)
    if "cqrs" in blob or "command" in blob and "query" in blob:
        p = add_if("CQRS", "architecture", 0.22)
        if p:
            out.append(p)
    if "hexagonal" in blob or "port" in blob and "adapter" in blob:
        p = add_if("Hexagonal Architecture", "architecture", 0.2)
        if p:
            out.append(p)
    if "clean" in blob and "architecture" in blob:
        p = add_if("Clean Architecture", "architecture", 0.2)
        if p:
            out.append(p)

    out.sort(key=lambda x: x.confidence, reverse=True)
    return out[:top_n]


def recognize_patterns_for_scope(
    *,
    facts: StructureFacts,
    llm: Any,
    store: WeaviatePatternInterpretStore,
    embedding_dim: int,
    language: str,
    scope_type: str,
    target_id: str,
    top_n: int = 12,
    min_confidence: float = 0.0,
    llm_timeout_seconds: Optional[int] = None,
) -> list[RecognizedPattern]:
    """对给定 scope_type + target_id 识别并写入 Weaviate。"""
    allowed_design, allowed_arch = allowed_pattern_names()
    allowed_design_set = set(allowed_design)
    allowed_arch_set = set(allowed_arch)

    if scope_type == "system":
        context = build_system_pattern_context(facts)
    elif scope_type == "module":
        context = build_module_pattern_context(facts, module_id=target_id)
    else:
        raise ValueError(f"Unsupported scope_type: {scope_type}")

    prompt = _build_prompt(
        language=language,
        scope_type=scope_type,
        target_id=target_id,
        top_n=top_n,
        context=context,
    )

    raw_text = ""
    try:
        gen_kwargs = {}
        if llm_timeout_seconds is not None:
            gen_kwargs["timeout"] = int(llm_timeout_seconds)
        raw_text = llm.generate(prompt, **gen_kwargs)
    except Exception as e:
        _LOG.warning("LLM generate 失败（fallback）: %s", e)
        patterns = _heuristic_fallback(
            facts=facts,
            scope_type=scope_type,
            target_id=target_id,
            top_n=top_n,
            language=language,
        )
    else:
        data = _extract_json(raw_text)
        patterns = _validate_and_normalize_patterns(
            data,
            allowed_design=allowed_design_set,
            allowed_arch=allowed_arch_set,
        )
        if not patterns:
            patterns = _heuristic_fallback(
                facts=facts,
                scope_type=scope_type,
                target_id=target_id,
                top_n=top_n,
                language=language,
            )

    # 简单置信度过滤（兜底可能包含低置信度候选）
    patterns = [p for p in patterns if p.confidence >= float(min_confidence)]
    # 写入
    for p in patterns[:top_n]:
        vec_text = f"[{p.pattern_type}] {p.pattern_name}\n置信度={p.confidence}\n{p.summary}\n证据说明={p.evidence.notes}"
        vec = get_embedding(vec_text, embedding_dim)
        try:
            evidence = {"entity_ids": p.evidence.entity_ids, "notes": p.evidence.notes}
            evidence_json = json.dumps(evidence, ensure_ascii=False)
        except Exception:
            evidence_json = str(p.evidence)
        store.add(
            vec,
            scope_type=scope_type,
            target_id=target_id,
            pattern_type=p.pattern_type,
            pattern_name=p.pattern_name,
            confidence=p.confidence,
            summary_text=p.summary,
            evidence_json=evidence_json,
            language=language,
            related_entity_ids_json=json.dumps(p.evidence.entity_ids, ensure_ascii=False),
        )
    return patterns


def recognize_patterns_system_and_modules(
    *,
    facts: StructureFacts,
    llm: Any,
    store: WeaviatePatternInterpretStore,
    embedding_dim: int,
    language: str,
    recognize_system: bool,
    recognize_modules: bool,
    module_ids: list[str],
    top_n: int,
    min_confidence: float = 0.0,
    skip_if_exists: bool = True,
    llm_timeout_seconds: Optional[int] = None,
) -> dict[str, Any]:
    """执行 system + module 的识别，并返回结构化结果（用于 UI）。"""
    out: dict[str, Any] = {"system": None, "modules": {}}

    if recognize_system:
        existing = store.list_by_scope(scope_type="system", target_id="system", limit=1)
        if not (skip_if_exists and existing):
            patterns = recognize_patterns_for_scope(
                facts=facts,
                llm=llm,
                store=store,
                embedding_dim=embedding_dim,
                language=language,
                scope_type="system",
                target_id="system",
                top_n=top_n,
                min_confidence=min_confidence,
                llm_timeout_seconds=llm_timeout_seconds,
            )
        else:
            patterns = []
        # UI 统一从库拉取
        sys_rows = store.list_by_scope(scope_type="system", target_id="system", limit=top_n * 3)
        sys_rows.sort(key=lambda x: float(x.get("confidence") or 0.0), reverse=True)
        out["system"] = sys_rows[:top_n]

    if recognize_modules:
        for mid in module_ids:
            rows = store.list_by_scope(scope_type="module", target_id=mid, limit=1)
            if skip_if_exists and rows:
                _rows = store.list_by_scope(scope_type="module", target_id=mid, limit=top_n * 3)
                _rows.sort(key=lambda x: float(x.get("confidence") or 0.0), reverse=True)
                out["modules"][mid] = _rows[:top_n]
                continue

            _ = recognize_patterns_for_scope(
                facts=facts,
                llm=llm,
                store=store,
                embedding_dim=embedding_dim,
                language=language,
                scope_type="module",
                target_id=mid,
                top_n=top_n,
                min_confidence=min_confidence,
                llm_timeout_seconds=llm_timeout_seconds,
            )
            _rows = store.list_by_scope(scope_type="module", target_id=mid, limit=top_n * 3)
            _rows.sort(key=lambda x: float(x.get("confidence") or 0.0), reverse=True)
            out["modules"][mid] = _rows[:top_n]

    return out

