"""知识层 OWL 本体与推理：从图导出 OWL/RDF，内置传递闭包推理，可选 HermiT 推理。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Generator, List, Optional, Tuple

# 传递性关系：可做传递闭包推理（与图中 rel_type 字符串一致）
TRANSITIVE_REL_TYPES = frozenset({
    "calls", "extends", "implements", "depends_on", "belongs_to",
    "service_calls", "BELONGS_TO_DOMAIN", "CONTAINS_CAPABILITY",
})

# 本体命名空间
CODE_NS = "http://knowledge-engineering.example.org/code#"
DOMAIN_NS = "http://knowledge-engineering.example.org/domain#"


def _safe_uri_local(s: str) -> str:
    """将节点 id 转为 URI 安全局部名（替换 :/# 等）。"""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s or "")


def graph_to_owl(kg: Any, base_uri: str = "http://knowledge-engineering.example.org/") -> Any:
    """
    从 KnowledgeGraph 导出 OWL/RDF 图（rdflib.Graph）。
    需要安装: pip install rdflib
    """
    try:
        from rdflib import Graph, Literal, Namespace, URIRef
        from rdflib.namespace import OWL, RDF, RDFS
    except ImportError as e:
        raise ImportError("导出 OWL 需要安装 rdflib: pip install rdflib") from e

    code = Namespace(CODE_NS)
    domain = Namespace(DOMAIN_NS)
    g = Graph()
    g.bind("code", code)
    g.bind("domain", domain)
    g.bind("owl", OWL)
    g.bind("rdfs", RDFS)

    # 声明实体类型为 OWL 类（从图中出现的 entity_type 动态收集）
    seen_types: set[str] = set()
    for nid, attrs in kg.iter_nodes():
        t = (attrs.get("entity_type") or "").strip()
        if t and t not in seen_types:
            seen_types.add(t)
            class_uri = code[t] if _is_code_entity_type(t) else domain[t]
            g.add((class_uri, RDF.type, OWL.Class))
            g.add((class_uri, RDFS.label, Literal(t)))

    # 声明关系类型为 OWL 对象属性
    seen_rels: set[str] = set()
    for _u, _v, rel_type, _ed in kg.iter_edges():
        if rel_type and rel_type not in seen_rels:
            seen_rels.add(rel_type)
            prop_uri = code[rel_type]
            g.add((prop_uri, RDF.type, OWL.ObjectProperty))
            g.add((prop_uri, RDFS.label, Literal(rel_type)))
            if rel_type in TRANSITIVE_REL_TYPES:
                g.add((prop_uri, RDF.type, OWL.TransitiveProperty))

    # 个体与类型
    for nid, attrs in kg.iter_nodes():
        local = _safe_uri_local(nid)
        ind_uri = URIRef(base_uri.rstrip("/") + "/id/" + local)
        etype = (attrs.get("entity_type") or "Thing").strip()
        class_uri = code[etype] if _is_code_entity_type(etype) else domain[etype]
        if (class_uri, RDF.type, OWL.Class) in g:
            g.add((ind_uri, RDF.type, class_uri))
        else:
            g.add((class_uri, RDF.type, OWL.Class))
            g.add((ind_uri, RDF.type, class_uri))
        name = attrs.get("name")
        if name is not None:
            g.add((ind_uri, RDFS.label, Literal(str(name))))

    # 边 -> 对象属性断言
    for u, v, rel_type, _ed in kg.iter_edges():
        if not rel_type:
            continue
        u_local = _safe_uri_local(u)
        v_local = _safe_uri_local(v)
        subj = URIRef(base_uri.rstrip("/") + "/id/" + u_local)
        obj = URIRef(base_uri.rstrip("/") + "/id/" + v_local)
        prop = code[rel_type]
        g.add((subj, prop, obj))

    return g


def _is_code_entity_type(t: str) -> bool:
    code_types = {
        "file", "module", "package", "class", "interface", "method",
        "field", "parameter", "service", "api_endpoint",
    }
    return (t or "").lower() in code_types


def run_builtin_reasoner(
    kg: Any,
    transitive_relations: Optional[frozenset[str]] = None,
    max_depth: int = 100,
) -> Generator[Tuple[str, str, str], None, None]:
    """
    内置推理：对指定关系类型做传递闭包，产出 (source_id, target_id, rel_type) 推断边。
    不修改图，仅生成可写回的边列表。
    """
    rels = transitive_relations or TRANSITIVE_REL_TYPES
    # 构建邻接表：rel_type -> (source -> [targets])
    adj: dict[str, dict[str, list[str]]] = {}
    for u, v, rel_type, _ in kg.iter_edges():
        if rel_type not in rels:
            continue
        r = rel_type if rel_type in rels else rel_type
        if r not in adj:
            adj[r] = {}
        if u not in adj[r]:
            adj[r][u] = []
        adj[r][u].append(v)

    # 对每种关系做传递闭包，去重后 yield
    seen: set[Tuple[str, str, str]] = set()
    for u, _v, rel_type, _ in kg.iter_edges():
        if rel_type not in rels:
            continue
        seen.add((u, _v, rel_type))

    for rel_type, out_map in adj.items():
        for start in list(out_map.keys()):
            closure: set[str] = set()
            stack = [start]
            depth = 0
            while stack and depth < max_depth:
                depth += 1
                cur = stack.pop()
                if cur in closure:
                    continue
                closure.add(cur)
                for w in out_map.get(cur, []):
                    if w not in closure:
                        stack.append(w)
            for end in closure:
                if start != end and (start, end, rel_type) not in seen:
                    seen.add((start, end, rel_type))
                    yield start, end, rel_type


def write_inferred_edges_to_graph(kg: Any, inferred: List[Tuple[str, str, str]]) -> int:
    """将推理得到的边写回 KnowledgeGraph（带 inferred=True）。返回写入条数。"""
    n = 0
    for s, t, r in inferred:
        kg.add_inferred_edge(s, t, r)
        n += 1
    return n


def export_owl_to_file(owl_graph: Any, path: str | Path, format: str = "turtle") -> Path:
    """将 rdflib Graph 导出为文件。format: turtle | xml | pretty-xml | json-ld。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    owl_graph.serialize(destination=str(p), format=format)
    return p


def run_ontology_pipeline(
    kg: Any,
    *,
    export_owl: bool = True,
    export_path: Optional[str | Path] = None,
    run_reasoner: str = "builtin",
    write_inferred_to_graph: bool = True,
) -> dict[str, Any]:
    """
    执行本体流水线：导出 OWL（可选）、运行推理（builtin 或 hermit）、可选写回图。
    kg 可为 KnowledgeGraph（内存图）或 Neo4jGraphBackend（Neo4j），需提供 iter_nodes、iter_edges、add_inferred_edge。
    返回统计与消息。
    """
    result: dict[str, Any] = {
        "export_owl": export_owl,
        "export_path": None,
        "reasoner": run_reasoner,
        "inferred_count": 0,
        "written_to_graph": 0,
        "errors": [],
    }

    if export_owl:
        try:
            owl_g = graph_to_owl(kg)
            if export_path:
                out = Path(export_path)
                export_owl_to_file(owl_g, out, format="turtle")
                result["export_path"] = str(out)
        except Exception as e:
            result["errors"].append(f"导出 OWL 失败: {e!r}")

    inferred: List[Tuple[str, str, str]] = []
    if run_reasoner == "builtin":
        try:
            inferred = list(run_builtin_reasoner(kg))
            result["inferred_count"] = len(inferred)
        except Exception as e:
            result["errors"].append(f"内置推理失败: {e!r}")
    elif run_reasoner == "hermit":
        # 可选：若已导出 OWL 文件，可在此调用 HermiT（需 Java + owlready2 或子进程）
        result["errors"].append("hermit 推理需额外配置 Java 与 HermiT，当前仅支持 builtin")

    if write_inferred_to_graph and inferred:
        try:
            result["written_to_graph"] = write_inferred_edges_to_graph(kg, inferred)
        except Exception as e:
            result["errors"].append(f"写回推理边失败: {e!r}")

    return result
