"""场景样板间子上下文：图谱 / 向量与解读 / 工程配置，由 SceneTemplateContext 组合。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from src.app.services.weaviate_data_service import WeaviateDataService
from src.knowledge.method_entity_id_normalize import method_entity_id_variants
from src.app.views.scene_template_room.scene_config_view import SceneTemplateConfigView
from src.knowledge.method_table_access_service import MethodTableAccessService
from src.knowledge.weaviate_business_store import WeaviateBusinessInterpretStore
from src.knowledge.weaviate_interpretation_store import WeaviateMethodInterpretStore


def _normalize_prefixed_id(value: str, *, prefix: str) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    if v.startswith(prefix):
        return v
    return f"{prefix}{v}"


@dataclass(frozen=True)
class SceneGraphContext:
    """图后端选择与节点查询（拓扑优先 / 内存优先 / 双源合并）。"""

    graph_backend: Any | None
    neo4j_backend: Any | None

    def get_graph_backend_memory_first(self) -> Any | None:
        return self.graph_backend or self.neo4j_backend

    def get_graph_backend_topology_primary(self) -> Any | None:
        if self.neo4j_backend is not None:
            return self.neo4j_backend
        return self.graph_backend

    def get_graph_backend_topology_merge_secondary(self) -> Any | None:
        primary = self.get_graph_backend_topology_primary()
        g = self.graph_backend
        if g is not None and primary is not None and g is not primary:
            return g
        return None

    def get_backend(self) -> Any | None:
        return self.get_graph_backend_memory_first()

    def get_calls_graph_backend(self) -> Any | None:
        return self.get_graph_backend_topology_primary()

    def has_graph_backend(self) -> bool:
        return self.graph_backend is not None or self.neo4j_backend is not None

    def get_node(self, nid: str) -> Optional[dict]:
        if not nid:
            return None
        backends: list[Any] = []
        for b in (self.neo4j_backend, self.graph_backend):
            if b is not None and b not in backends:
                backends.append(b)
        if not backends:
            return None

        def _try(getter: Any, node_id: str) -> Optional[dict]:
            if not callable(getter):
                return None
            n = getter(node_id)
            return n if n is not None else None

        for b in backends:
            getter = getattr(b, "get_node", None)
            n = _try(getter, nid)
            if n is not None:
                return n
            for alt in method_entity_id_variants(nid):
                if alt == nid:
                    continue
                n2 = _try(getter, alt)
                if n2 is not None:
                    return n2
        return None

    def node_module_id(self, nid: str) -> str | None:
        n = self.get_node(nid) or {}
        mid = n.get("module_id")
        return str(mid) if mid else None

    def resolve_method_id(
        self, method_input: str, *, module_filter: str | None = None
    ) -> Optional[str]:
        v = (method_input or "").strip()
        if not v:
            return None
        if v.startswith("method://"):
            nm = self.node_module_id(v)
            if module_filter and nm and module_filter not in (nm or ""):
                return None
            return v
        b = self.get_graph_backend_topology_primary()
        if b is None or not hasattr(b, "search_by_name"):
            return None
        try:
            hits = b.search_by_name(v, entity_types=["method"], limit=30)  # type: ignore[attr-defined]
        except Exception:
            hits = []
        if module_filter:
            mf = str(module_filter).strip()
            hits = [h for h in hits if mf and (str(h.get("module_id") or "") == mf)]
        if not hits:
            return None
        return str(hits[0].get("id") or "")


@dataclass(frozen=True)
class SceneVectorsContext:
    """代码向量库、技术/业务解读 Weaviate 与统一片段拉取。"""

    weaviate_data_svc: WeaviateDataService
    code_vector_store: Any | None
    method_interpret_store: WeaviateMethodInterpretStore | None
    business_interpret_store: WeaviateBusinessInterpretStore | None

    def get_code_snippet(self, entity_id: str) -> str:
        return self.weaviate_data_svc.fetch_method_snippet(entity_id)


@dataclass(frozen=True)
class SceneProjectContext:
    """工程级配置与派生服务（如方法↔表）。"""

    repo_cfg: dict[str, Any]
    config_view: SceneTemplateConfigView
    method_table_access_svc: MethodTableAccessService | None = None


def capability_nid(capability_id: str) -> str:
    return _normalize_prefixed_id(capability_id, prefix="capability://")


def domain_nid(domain_id: str) -> str:
    return _normalize_prefixed_id(domain_id, prefix="domain://")


def term_nid(term_id: str) -> str:
    return _normalize_prefixed_id(term_id, prefix="term://")


def service_nid_from_service_id(service_id: str) -> str:
    v = (service_id or "").strip()
    if not v:
        return ""
    if v.startswith("service://"):
        return v
    return f"service://{v}"
