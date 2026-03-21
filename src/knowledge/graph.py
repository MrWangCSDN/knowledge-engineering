"""知识层：将结构事实 + 语义增强事实 写入图（内存 / Neo4j）；向量库（内存 / Weaviate）；快照。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

import networkx as nx

from src.models import (
    StructureFacts,
    SemanticFacts,
    DomainKnowledge,
    EntityType,
    RelationType,
)
from src.semantic.embedding import get_embedding
from src.knowledge.factories import VectorStoreFactory
from src.knowledge.vector_store import VectorStore


def _neo4j_sanitize(value: Any) -> Any:
    """将节点/边属性值转为 Neo4j 驱动支持的类型（不支持 set）。"""
    if value is None:
        return value
    if isinstance(value, set):
        return list(value)
    if isinstance(value, dict):
        return {k: _neo4j_sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_neo4j_sanitize(v) for v in value]
    return value


def _sync_graph_to_neo4j(
    g: nx.MultiDiGraph,
    uri: str,
    user: str,
    password: str,
    database: str = "neo4j",
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> None:
    """将内存图同步到 Neo4j。progress_callback(current, total, message) 用于前端进度条。"""
    from src.knowledge.factories import GraphBackendFactory
    backend = GraphBackendFactory.create(
        "neo4j",
        neo4j_uri=uri,
        neo4j_user=user,
        neo4j_password=password,
        neo4j_database=database,
    )
    try:
        if progress_callback:
            progress_callback(0, 1, "正在清空 Neo4j 旧数据…")
        backend.clear()
        nodes = list(g.nodes)
        edges = list(g.edges(keys=True))
        n_total, e_total = len(nodes), len(edges)
        total_steps = n_total + e_total
        step = 0
        for i, nid in enumerate(nodes):
            attrs = _neo4j_sanitize(dict(g.nodes[nid]))
            backend.add_node(nid, **attrs)
            step += 1
            if progress_callback and total_steps:
                progress_callback(step, total_steps, f"同步节点 {i + 1}/{n_total}")
        for i, (u, v, k) in enumerate(edges):
            ed = dict(g.edges[u, v, k])
            rel_type = ed.pop("rel_type", "RELATED")
            ed = _neo4j_sanitize(ed)
            backend.add_edge(u, v, rel_type=rel_type, **ed)
            step += 1
            if progress_callback and total_steps:
                progress_callback(step, total_steps, f"同步边 {i + 1}/{e_total}")
        if progress_callback and total_steps:
            progress_callback(total_steps, total_steps, "Neo4j 同步完成")
    finally:
        backend.close()


class KnowledgeGraph:
    """
    图存储：代码本体 + 业务本体 + 关联。
    图后端：memory (NetworkX) 或 neo4j（构建后同步到 Neo4j）。
    向量后端：memory 或 weaviate。
    """

    def __init__(self):
        self._g: nx.MultiDiGraph = nx.MultiDiGraph()
        self._node_attrs: dict[str, dict] = {}
        self._vector_store: Optional[VectorStore] = None
        self._version: Optional[str] = None

    def clear(self) -> None:
        self._g.clear()
        self._node_attrs.clear()
        if self._vector_store:
            if hasattr(self._vector_store, "clear"):
                self._vector_store.clear()
            if hasattr(self._vector_store, "close"):
                try:
                    self._vector_store.close()
                except Exception:
                    pass
        self._version = None

    def build_from(
        self,
        structure_facts: StructureFacts,
        semantic_facts: SemanticFacts,
        domain: DomainKnowledge,
        vector_enabled: bool = False,
        vector_dim: int = 64,
        graph_backend: str = "memory",
        vector_backend: str = "memory",
        graph_config: Optional[dict] = None,
        vector_config: Optional[dict] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> None:
        """从结构事实 + 语义增强事实 + 领域知识 构建图谱。progress_callback(current, total, message) 用于 Neo4j 同步进度。"""
        self.clear()

        # 结构实体 -> 节点（含 attributes，如 class_name、signature、path 等；code_snippet 仅存 Weaviate，不入图）
        for e in structure_facts.entities:
            attrs_no_snippet = {k: v for k, v in (e.attributes or {}).items() if k != "code_snippet"}
            node_attrs = {
                "entity_type": e.type.value,
                "name": e.name,
                "location": e.location,
                "module_id": e.module_id,
                **attrs_no_snippet,
            }
            node_attrs = {k: v for k, v in node_attrs.items() if v is not None}
            self._g.add_node(e.id, **node_attrs)
            self._node_attrs[e.id] = dict(node_attrs)

        # 结构关系 -> 边
        for r in structure_facts.relations:
            self._g.add_edge(r.source_id, r.target_id, rel_type=r.type.value, **r.attributes)

        # 业务域节点
        for d in domain.business_domains:
            nid = f"domain://{d.id}"
            self._g.add_node(nid, entity_type="BusinessDomain", name=d.name or d.id)
            self._node_attrs[nid] = {"entity_type": "BusinessDomain", "name": d.name or d.id}

        # 业务能力节点
        for c in domain.capabilities:
            cid = c.get("id")
            if not cid:
                continue
            nid = f"capability://{cid}"
            self._g.add_node(nid, entity_type="BusinessCapability", name=c.get("name", cid))
            self._node_attrs[nid] = {"entity_type": "BusinessCapability", "name": c.get("name", cid)}

        # 业务术语节点
        for t in domain.terms:
            tid = t.get("id")
            if not tid:
                continue
            nid = f"term://{tid}"
            self._g.add_node(nid, entity_type="BusinessTerm", name=t.get("name", tid))
            self._node_attrs[nid] = {"entity_type": "BusinessTerm", "name": t.get("name", tid)}

        # 业务域 — 包含 — 业务能力（CONTAINS_CAPABILITY）
        for d in domain.business_domains:
            domain_nid = f"domain://{d.id}"
            if not self._g.has_node(domain_nid):
                continue
            for cap_id in d.capability_ids or []:
                cap_nid = f"capability://{cap_id}"
                if self._g.has_node(cap_nid):
                    self._g.add_edge(domain_nid, cap_nid, rel_type="CONTAINS_CAPABILITY")

        # 服务—承载—业务域
        for m in domain.service_domain_mappings:
            sid = f"service://{m.service_or_module_id}"
            if not self._g.has_node(sid):
                self._g.add_node(sid, entity_type="Service", name=m.service_or_module_id)
                self._node_attrs[sid] = {"entity_type": "Service", "name": m.service_or_module_id}
            for did in m.business_domain_ids:
                domain_id = f"domain://{did}"
                if self._g.has_node(domain_id):
                    self._g.add_edge(sid, domain_id, rel_type="BELONGS_TO_DOMAIN", weight=m.weight)

        # 语义：业务概念 — 实现于/涉及 — 代码实体
        for se in semantic_facts.semantic_entities:
            for link in se.business_links:
                if link.link_type == "implemented_by":
                    cap_id = f"capability://{link.business_concept_id}"
                    if self._g.has_node(cap_id):
                        self._g.add_edge(
                            cap_id, se.structure_entity_id,
                            rel_type="IMPLEMENTED_BY", confidence=link.confidence, source=link.source
                        )
                else:
                    # related_to: 连到 domain 或 term
                    domain_id = f"domain://{link.business_concept_id}"
                    term_id = f"term://{link.business_concept_id}"
                    if self._g.has_node(term_id) and self._g.has_node(se.structure_entity_id):
                        self._g.add_edge(
                            term_id, se.structure_entity_id,
                            rel_type="RELATED_TO", confidence=link.confidence
                        )
                    elif self._g.has_node(domain_id) and self._g.has_node(se.structure_entity_id):
                        self._g.add_edge(
                            domain_id, se.structure_entity_id,
                            rel_type="RELATED_TO", confidence=link.confidence
                        )

        # 代码实体 — 归属业务域（IN_DOMAIN）：沿 belongs_to 回溯到服务，再沿 BELONGS_TO_DOMAIN 到业务域
        def find_service_id(start: str) -> Optional[str]:
            visited: set = set()
            stack = [start]
            while stack:
                cur = stack.pop()
                if cur in visited:
                    continue
                visited.add(cur)
                if str(cur).startswith("service://"):
                    return cur
                for u, v, k in self._g.in_edges(cur, keys=True):
                    if v != cur:
                        continue
                    ed = self._g.edges[u, v, k]
                    if isinstance(ed, dict) and ed.get("rel_type") == "belongs_to":
                        stack.append(u)
                        break
            return None

        for e in structure_facts.entities:
            if e.type.value not in ("class", "interface", "method"):
                continue
            eid = e.id
            if not self._g.has_node(eid):
                continue
            service_id = find_service_id(eid)
            if not service_id:
                continue
            for _, domain_nid, k in self._g.out_edges(service_id, keys=True):
                ed = self._g.edges[service_id, domain_nid, k]
                if isinstance(ed, dict) and ed.get("rel_type") == "BELONGS_TO_DOMAIN" and self._g.has_node(domain_nid):
                    self._g.add_edge(eid, domain_nid, rel_type="IN_DOMAIN", derived=True)

        if vector_enabled:
            vc = vector_config or {}
            self._vector_store = VectorStoreFactory.create(
                vc.get("backend", "memory"),
                True,
                vector_dim,
                allow_fallback_to_memory=bool(vc.get("allow_fallback_to_memory", False)),
                weaviate_url=vc.get("weaviate_url"),
                weaviate_grpc_port=vc.get("weaviate_grpc_port"),
                collection_name=vc.get("collection_name"),
                weaviate_api_key=vc.get("weaviate_api_key"),
            )
            if self._vector_store:
                # 有 code_snippet 的方法单独用代码向量写入，与图谱方法节点 entity_id 一致
                method_ids_with_snippet = {
                    e.id for e in structure_facts.entities
                    if e.type == EntityType.METHOD and (e.attributes or {}).get("code_snippet")
                }
                for se in semantic_facts.semantic_entities:
                    if not se.embed_text:
                        continue
                    if se.structure_entity_id in method_ids_with_snippet:
                        continue
                    vec = get_embedding(se.embed_text, vector_dim)
                    self._vector_store.add(se.structure_entity_id, vec)
                for e in structure_facts.entities:
                    if e.type != EntityType.METHOD:
                        continue
                    snippet = (e.attributes or {}).get("code_snippet")
                    if not snippet:
                        continue
                    vec = get_embedding(snippet, vector_dim)
                    add_method = getattr(self._vector_store, "add", None)
                    if callable(add_method):
                        add_method(
                            e.id,
                            vec,
                            entity_type="method",
                            name=e.name or "",
                            code_snippet=snippet,
                        )

        # 当 graph.backend=neo4j 时，将内存图同步到 Neo4j（graph_config 为空则用默认连接参数）
        self._neo4j_sync_status: Optional[str] = None
        if graph_backend == "neo4j":
            gc = graph_config if isinstance(graph_config, dict) else {}
            try:
                _sync_graph_to_neo4j(
                    self._g,
                    uri=gc.get("neo4j_uri") or "bolt://localhost:7687",
                    user=gc.get("neo4j_user") or "neo4j",
                    password=gc.get("neo4j_password") or "password",
                    database=gc.get("neo4j_database") or "neo4j",
                    progress_callback=progress_callback,
                )
                self._neo4j_sync_status = "ok"
            except Exception as e:
                self._neo4j_sync_status = f"failed: {e!r}"
        else:
            self._neo4j_sync_status = "skipped"

    def node_count(self) -> int:
        return self._g.number_of_nodes()

    def edge_count(self) -> int:
        return self._g.number_of_edges()

    def iter_nodes(self):
        """迭代所有节点，产出 (node_id, attrs_dict)。供 OWL 导出等使用。"""
        for nid in self._g.nodes:
            yield nid, dict(self._g.nodes[nid])

    def iter_edges(self):
        """迭代所有边，产出 (source_id, target_id, rel_type, attrs_dict)。供 OWL 导出与推理使用。"""
        for u, v, key in self._g.edges(keys=True):
            ed = dict(self._g.edges[u, v, key])
            rel_type = ed.get("rel_type", "RELATED")
            yield u, v, rel_type, ed

    def add_inferred_edge(self, source_id: str, target_id: str, rel_type: str, **attrs: Any) -> None:
        """添加一条推理得到的边（如传递闭包）。若节点不存在则忽略。"""
        if self._g.has_node(source_id) and self._g.has_node(target_id):
            self._g.add_edge(source_id, target_id, rel_type=rel_type, inferred=True, **attrs)

    def get_node(self, nid: str) -> Optional[dict]:
        if not self._g.has_node(nid):
            return None
        data = dict(self._g.nodes[nid])
        data["id"] = nid
        return data

    def get_entity_code(self, entity_id: str) -> Optional[dict]:
        """从向量库（如 Weaviate）按 entity_id 取对应代码块，实现「从方法查源代码」双向关联。"""
        if not entity_id or not self._vector_store:
            return None
        getter = getattr(self._vector_store, "get_by_entity_id", None)
        if not callable(getter):
            return None
        return getter(entity_id)

    @staticmethod
    def _edge_rel_matches(edge_rel: Any, want: Optional[str]) -> bool:
        if want is None:
            return True
        return str(edge_rel or "").lower() == str(want).lower()

    def successors(self, nid: str, rel_type: Optional[str] = None) -> list[str]:
        if not self._g.has_node(nid):
            return []
        out = []
        for _, target, keys in self._g.out_edges(nid, keys=True):
            edata = self._g.edges[nid, target, keys]
            if self._edge_rel_matches(edata.get("rel_type"), rel_type):
                out.append(target)
        return out

    def successors_excluding_rel_types(
        self, nid: str, exclude_rel_types: tuple[str, ...] | list[str]
    ) -> list[str]:
        if not self._g.has_node(nid):
            return []
        exc = {str(x).strip().lower() for x in exclude_rel_types if str(x).strip()}
        if not exc:
            return self.successors(nid, rel_type=None)
        out: list[str] = []
        for _, target, keys in self._g.out_edges(nid, keys=True):
            edata = self._g.edges[nid, target, keys]
            rt = str(edata.get("rel_type") or "").strip().lower()
            if rt in exc:
                continue
            out.append(target)
        return out

    def predecessors(self, nid: str, rel_type: Optional[str] = None) -> list[str]:
        if not self._g.has_node(nid):
            return []
        out = []
        for src, _, keys in self._g.in_edges(nid, keys=True):
            edata = self._g.edges[src, nid, keys]
            if self._edge_rel_matches(edata.get("rel_type"), rel_type):
                out.append(src)
        return out

    def predecessors_excluding_rel_types(
        self, nid: str, exclude_rel_types: tuple[str, ...] | list[str]
    ) -> list[str]:
        if not self._g.has_node(nid):
            return []
        exc = {str(x).strip().lower() for x in exclude_rel_types if str(x).strip()}
        if not exc:
            return self.predecessors(nid, rel_type=None)
        out: list[str] = []
        for src, _, keys in self._g.in_edges(nid, keys=True):
            edata = self._g.edges[src, nid, keys]
            rt = str(edata.get("rel_type") or "").strip().lower()
            if rt in exc:
                continue
            out.append(src)
        return out

    def impact_closure(
        self,
        start_id: str,
        direction: str = "down",
        max_depth: int = 50,
        exclude_inferred: bool = False,
    ) -> set[str]:
        """
        影响闭包：从 start_id 出发沿边遍历（down=后继，up=前驱），返回可达节点集合。
        默认沿 MultiDiGraph 上**全部出边/入边**（含 calls、implements、belongs_to 等），不按关系类型过滤。
        exclude_inferred=True 时仅沿「非推断」边遍历，用于与全图闭包对比、体现推理价值。
        """
        seen: set[str] = set()
        stack = [start_id]
        depth = 0
        while stack and depth < max_depth:
            depth += 1
            nid = stack.pop()
            if nid in seen:
                continue
            seen.add(nid)
            if direction == "down":
                if exclude_inferred:
                    next_ids = [
                        t for _, t, k in self._g.out_edges(nid, keys=True)
                        if not self._g.edges[nid, t, k].get("inferred")
                    ]
                else:
                    next_ids = list(self._g.successors(nid))
            else:
                if exclude_inferred:
                    next_ids = [
                        s for s, _, k in self._g.in_edges(nid, keys=True)
                        if not self._g.edges[s, nid, k].get("inferred")
                    ]
                else:
                    next_ids = list(self._g.predecessors(nid))
            for k in next_ids:
                if k not in seen:
                    stack.append(k)
        return seen

    def subgraph_for_service(self, service_id: str) -> dict[str, Any]:
        """返回某服务及其包含的节点、边的子图。有 Service 节点则从其出发做闭包；否则按 module_id 取子图。"""
        sid = service_id if (service_id or "").startswith("service://") else f"service://{service_id}"
        nodes_in = set()
        if self._g.has_node(sid):
            nodes_in = self.impact_closure(sid, direction="down", max_depth=10)
            nodes_in.add(sid)
        if not nodes_in:
            # 无 Service 节点时按 module_id 兜底
            module_id = sid.replace("service://", "").strip() if sid else ""
            if module_id:
                for nid in self._g.nodes:
                    if (self._g.nodes[nid].get("module_id") or "") == module_id:
                        nodes_in.add(nid)
        if not nodes_in:
            return {"nodes": [], "edges": []}
        sub = self._g.subgraph(nodes_in)
        nodes = [{"id": n, **dict(sub.nodes[n])} for n in sub.nodes()]
        edges = [
            {"source": u, "target": v, **dict(d)}
            for u, v, d in sub.edges(data=True)
        ]
        return {"nodes": nodes, "edges": edges}

    def _find_method_node_ids(self, class_name: str, method_name: str) -> list[str]:
        """按类名+方法名查找所有 method 节点 id（含重载）。"""
        out = []
        for nid in self._g.nodes:
            data = dict(self._g.nodes[nid])
            if (data.get("entity_type") or "").lower() != "method":
                continue
            if (data.get("class_name") or "") != class_name or (data.get("name") or "") != method_name:
                continue
            out.append(nid)
        return out

    def get_direct_callees(self, class_name: str, method_name: str) -> list[dict]:
        """
        给定类名+方法名，返回该方法直接调用的其他方法列表。
        每项为 {"class_name": str, "method_name": str}。
        基于内存图；若需查 Neo4j 可直接用 Neo4jGraphBackend.query_direct_callees。
        """
        nids = self._find_method_node_ids(class_name, method_name)
        seen: set[tuple[str, str]] = set()
        result: list[dict] = []
        for nid in nids:
            for _, target, keys in self._g.out_edges(nid, keys=True):
                ed = self._g.edges[nid, target, keys]
                if (ed.get("rel_type") or "").lower() != "calls":
                    continue
                tdata = dict(self._g.nodes[target])
                cname = (tdata.get("class_name") or "").strip()
                mname = (tdata.get("name") or "").strip()
                if (cname, mname) in seen:
                    continue
                seen.add((cname, mname))
                result.append({"class_name": cname, "method_name": mname})
        return result

    def get_direct_callers(self, class_name: str, method_name: str) -> list[dict]:
        """
        给定类名+方法名，返回所有直接调用该方法的其他方法列表。
        每项为 {"class_name": str, "method_name": str}。
        """
        nids = self._find_method_node_ids(class_name, method_name)
        seen: set[tuple[str, str]] = set()
        result: list[dict] = []
        for nid in nids:
            for src, _, keys in self._g.in_edges(nid, keys=True):
                ed = self._g.edges[src, nid, keys]
                if (ed.get("rel_type") or "").lower() != "calls":
                    continue
                sdata = dict(self._g.nodes[src])
                cname = (sdata.get("class_name") or "").strip()
                mname = (sdata.get("name") or "").strip()
                if (cname, mname) in seen:
                    continue
                seen.add((cname, mname))
                result.append({"class_name": cname, "method_name": mname})
        return result

    def search_by_name(self, name_substring: str, entity_types: Optional[list[str]] = None) -> list[dict]:
        """按名称模糊搜索节点。"""
        name_lower = name_substring.lower()
        out = []
        for nid in self._g.nodes:
            data = dict(self._g.nodes[nid])
            if name_lower not in (data.get("name") or "").lower():
                continue
            if entity_types and (data.get("entity_type") or "").lower() not in [t.lower() for t in entity_types]:
                continue
            out.append({"id": nid, **data})
        return out

    def similarity_search(self, query_text: str, top_k: int = 10) -> list[dict]:
        """基于向量库的语义相似检索，返回与 query 最相关的实体及得分。"""
        if not self._vector_store or self._vector_store.size() == 0:
            return []
        hits = self._vector_store.search_by_text(query_text, top_k=top_k)
        out = []
        for nid, score in hits:
            node = self.get_node(nid)
            if node:
                node["similarity_score"] = round(score, 4)
                out.append(node)
        return out

    def save_snapshot(self, output_dir: str | Path, version: str = "default") -> Path:
        """将当前图与元数据保存为快照目录（graph.json + meta.json）。"""
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        from networkx.readwrite import node_link_data
        data = node_link_data(self._g, edges="links")
        (out / "graph.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        meta = {"version": version, "nodes": self._g.number_of_nodes(), "edges": self._g.number_of_edges()}
        (out / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        self._version = version
        return out

    def load_snapshot(self, snapshot_dir: str | Path) -> None:
        """从快照目录加载图，覆盖当前图。"""
        from networkx.readwrite import node_link_graph
        path = Path(snapshot_dir)
        data = json.loads((path / "graph.json").read_text(encoding="utf-8"))
        self.clear()
        self._g = node_link_graph(data, directed=True, multigraph=True, edges="links")
        meta_path = path / "meta.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self._version = meta.get("version")
        for nid in self._g.nodes:
            self._node_attrs[nid] = dict(self._g.nodes[nid])

    @property
    def version(self) -> Optional[str]:
        return self._version
