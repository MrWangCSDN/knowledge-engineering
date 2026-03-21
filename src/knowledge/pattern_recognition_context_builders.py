"""从 structure_facts 构建模式识别所需证据上下文。"""

from __future__ import annotations

import re
from collections import Counter
from typing import Iterable, Optional

from src.models.structure import EntityType, RelationType, StructureEntity, StructureFacts


_DESIGN_KEYWORDS: list[tuple[str, str]] = [
    ("Singleton", "Singleton"),
    ("getInstance", "Singleton"),
    ("instance", "Singleton"),  # 兜底信号：后续会通过上下文裁剪
    ("Factory", "Factory Method"),
    ("AbstractFactory", "Abstract Factory"),
    ("Builder", "Builder"),
    ("Prototype", "Prototype"),
    ("Adapter", "Adapter"),
    ("Decorator", "Decorator"),
    ("Facade", "Facade"),
    ("Bridge", "Bridge"),
    ("Composite", "Composite"),
    ("Flyweight", "Flyweight"),
    ("Proxy", "Proxy"),
    ("Chain", "Chain of Responsibility"),
    ("ChainOfResponsibility", "Chain of Responsibility"),
    ("Command", "Command"),
    ("Mediator", "Mediator"),
    ("Iterator", "Iterator"),
    ("Template", "Template Method"),
    ("Observer", "Observer"),
    ("Listener", "Observer"),
    ("State", "State"),
    ("Strategy", "Strategy"),
    ("Visitor", "Visitor"),
    ("Memento", "Memento"),
    ("Interpreter", "Interpreter"),
]


def _index_entities(facts: StructureFacts) -> dict[str, StructureEntity]:
    return {e.id: e for e in facts.entities}


def _safe_one_line(s: str, limit: int) -> str:
    s = (s or "").strip().replace("\r", " ").replace("\n", " ")
    if len(s) > limit:
        return s[: limit - 1] + "…"
    return s


def _count_by_type(facts: StructureFacts, entity_types: Iterable[EntityType]) -> dict[str, int]:
    ts = set(entity_types)
    c: dict[str, int] = {}
    for e in facts.entities:
        if e.type in ts:
            c[e.type.value] = c.get(e.type.value, 0) + 1
    return c


def _sample_module_edges(facts: StructureFacts, *, max_edges: int = 10) -> list[str]:
    """将跨模块关系样本化，用于架构推断。"""
    id_to = _index_entities(facts)
    module_edge_counter: Counter[str] = Counter()
    for r in facts.relations:
        if r.type not in (RelationType.CALLS, RelationType.DEPENDS_ON, RelationType.EXTENDS, RelationType.IMPLEMENTS):
            continue
        s = id_to.get(r.source_id)
        t = id_to.get(r.target_id)
        if not s or not t:
            continue
        if not s.module_id or not t.module_id:
            continue
        if s.module_id == t.module_id:
            continue
        k = f"{s.module_id} -> {t.module_id}"
        module_edge_counter[k] += 1

    items = module_edge_counter.most_common(max_edges)
    return [f"- {k}  （{v} 条关系证据）" for k, v in items]


def _entry_points(facts: StructureFacts, *, max_paths: int = 12) -> list[str]:
    """提取带 HTTP path 的方法，帮助架构（MVC/Controller）推断。"""
    paths: Counter[str] = Counter()
    for e in facts.entities:
        if e.type != EntityType.METHOD:
            continue
        attrs = e.attributes or {}
        path = attrs.get("path") or ""
        if not isinstance(path, str) or not path.strip():
            continue
        mod = e.module_id or ""
        key = f"{path}（{mod}）" if mod else path
        paths[key] += 1
    return [f"- {k}" for k, _ in paths.most_common(max_paths)]


def _collect_design_hint_entities(
    facts: StructureFacts, *, in_module: Optional[str], max_entities: int = 10
) -> list[StructureEntity]:
    """根据类/接口/方法名称关键词收集“可能相关”的证据实体。"""
    kw_to_pattern: list[tuple[re.Pattern[str], str]] = []
    for kw, _pattern in _DESIGN_KEYWORDS:
        kw_to_pattern.append((re.compile(re.escape(kw), re.IGNORECASE), _pattern))

    hint: list[tuple[int, StructureEntity]] = []
    for e in facts.entities:
        if e.type not in (EntityType.CLASS, EntityType.INTERFACE):
            continue
        if in_module and e.module_id != in_module:
            continue
        name = e.name or ""
        score = 0
        for rx, _pat in kw_to_pattern:
            if rx.search(name or ""):
                score += 1
        # 也尝试从模块名里找信号（有些工程把 pattern 关键字放在模块名）
        if not score and in_module:
            if any(rx.search(in_module or "") for rx, _pat in kw_to_pattern):
                score = 1
        if score > 0:
            hint.append((score, e))

    hint.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in hint[:max_entities]]


def _extract_method_excerpts(
    facts: StructureFacts,
    *,
    class_names: list[str],
    in_module: Optional[str],
    max_methods: int = 3,
    excerpt_chars: int = 900,
) -> list[str]:
    """提取少量方法体节选（仅用于 LLM 识别信号），避免 prompt 过长。"""
    class_set = set(class_names)
    out: list[str] = []
    for m in facts.entities:
        if m.type != EntityType.METHOD:
            continue
        if in_module and m.module_id != in_module:
            continue
        attrs = m.attributes or {}
        cls_name = attrs.get("class_name") or ""
        if cls_name not in class_set:
            continue
        snippet = attrs.get("code_snippet") or ""
        if not isinstance(snippet, str) or not snippet.strip():
            continue
        sig = attrs.get("signature") or m.name or ""
        out.append(
            f"- {m.id} | {cls_name}.{m.name or ''}\n  signature: {_safe_one_line(sig, 140)}\n  excerpt: {snippet[:excerpt_chars].replace(chr(10), ' ') + ('…' if len(snippet) > excerpt_chars else '')}"
        )
        if len(out) >= max_methods:
            break
    return out


def build_system_pattern_context(facts: StructureFacts, *, max_hint_entities: int = 10) -> str:
    """系统级证据上下文（面向 LLM）。"""
    entity_counts = _count_by_type(facts, [EntityType.MODULE, EntityType.PACKAGE, EntityType.CLASS, EntityType.INTERFACE, EntityType.METHOD])
    module_ids = sorted({e.module_id for e in facts.entities if e.module_id})
    modules_line = ", ".join(module_ids[:12]) + (f"…（共 {len(module_ids)} 个）" if len(module_ids) > 12 else "")

    edges = _sample_module_edges(facts, max_edges=10)
    entry = _entry_points(facts, max_paths=12)

    hint_entities = _collect_design_hint_entities(facts, in_module=None, max_entities=max_hint_entities)
    hint_lines = [
        f"- {e.id} | {e.name or ''} | module={e.module_id or ''}"
        for e in hint_entities
    ]

    # 从“提示类”中抽少量 method 代码节选
    class_names = [e.name for e in hint_entities if e.name][:8]
    method_excerpts = _extract_method_excerpts(facts, class_names=class_names, in_module=None, max_methods=3)

    return "\n".join(
        [
            "=== System Evidence (structure_facts) ===",
            f"- modules: {modules_line}",
            "- entity counts (selected types): "
            + ", ".join([f"{k}={v}" for k, v in sorted(entity_counts.items())]),
            "",
            "=== Cross-module dependency samples ===",
            *(edges or ["- （无明显跨模块依赖证据）"]),
            "",
            "=== Entry points (HTTP-like paths) ===",
            *(entry or ["- （无 path 属性方法）"]),
            "",
            "=== Design-pattern hint entities (name keywords) ===",
            *(hint_lines or ["- （无明显关键词命中）"]),
            "",
            "=== Method code excerpts (from hint classes) ===",
            *(method_excerpts or ["- （无 code_snippet 节选）"]),
        ]
    )


def build_module_pattern_context(facts: StructureFacts, *, module_id: str, max_hint_entities: int = 10) -> str:
    """模块级证据上下文（面向 LLM）。"""
    sub_entities = [e for e in facts.entities if e.module_id == module_id]
    if not sub_entities:
        return f"=== Module Evidence ===\n- module_id={module_id}\n- （无该 module 的实体）"

    # 上下文里只抽样输出证据：为了避免 prompt 过长，只在后面抽关系样本时再遍历全量 facts.relations 并过滤 module_id

    entity_counts = _count_by_type(facts, [EntityType.CLASS, EntityType.INTERFACE, EntityType.METHOD])
    # 以上 entity_counts 是全局的；这里需要模块内统计，重算一次
    ts = {EntityType.CLASS, EntityType.INTERFACE, EntityType.METHOD}
    mod_counts: dict[str, int] = {}
    for e in sub_entities:
        if e.type in ts:
            mod_counts[e.type.value] = mod_counts.get(e.type.value, 0) + 1

    # 模块间边样本（只要进出该 module）
    id_to = _index_entities(facts)
    edge_counter: Counter[str] = Counter()
    for r in facts.relations:
        if r.type not in (RelationType.CALLS, RelationType.DEPENDS_ON, RelationType.EXTENDS, RelationType.IMPLEMENTS):
            continue
        s = id_to.get(r.source_id)
        t = id_to.get(r.target_id)
        if not s or not t:
            continue
        if not s.module_id or not t.module_id:
            continue
        if s.module_id != module_id and t.module_id != module_id:
            continue
        if s.module_id == t.module_id:
            continue
        k = f"{s.module_id} -> {t.module_id}"
        edge_counter[k] += 1
    edges = [f"- {k}  （{v} 条关系证据）" for k, v in edge_counter.most_common(10)]

    # 模块内 entry paths
    entry: list[str] = []
    paths: Counter[str] = Counter()
    for e in sub_entities:
        if e.type != EntityType.METHOD:
            continue
        attrs = e.attributes or {}
        path = attrs.get("path") or ""
        if not isinstance(path, str) or not path.strip():
            continue
        paths[path] += 1
    entry = [f"- {p}" for p, _ in paths.most_common(12)]

    hint_entities = _collect_design_hint_entities(facts, in_module=module_id, max_entities=max_hint_entities)
    hint_lines = [f"- {e.id} | {e.name or ''}" for e in hint_entities]

    class_names = [e.name for e in hint_entities if e.name][:8]
    method_excerpts = _extract_method_excerpts(
        facts,
        class_names=class_names,
        in_module=module_id,
        max_methods=3,
    )

    return "\n".join(
        [
            f"=== Module Evidence (structure_facts) ===",
            f"- module_id: {module_id}",
            "- entity counts (selected types, module-local): "
            + ", ".join([f"{k}={v}" for k, v in sorted(mod_counts.items())]),
            "",
            "=== In/Out module dependency samples ===",
            *(edges or ["- （无明显跨模块依赖证据）"]),
            "",
            "=== Entry points (module-local paths) ===",
            *(entry or ["- （无 path 属性方法）"]),
            "",
            "=== Design-pattern hint entities (name keywords) ===",
            *(hint_lines or ["- （无明显关键词命中）"]),
            "",
            "=== Method code excerpts (from hint classes) ===",
            *(method_excerpts or ["- （无 code_snippet 节选）"]),
        ]
    )

