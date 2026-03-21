from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

import streamlit as st

from src.app.services.app_services import AppServices
from src.core.weaviate_defaults import (
    DEFAULT_COLLECTION_BUSINESS_INTERPRETATION,
    DEFAULT_COLLECTION_METHOD_INTERPRETATION,
    DEFAULT_WEAVIATE_GRPC_PORT,
    DEFAULT_WEAVIATE_HTTP_URL,
)
from src.knowledge.factories import VectorStoreFactory
from src.config.models import ProjectConfig
from src.knowledge.method_table_access_service import MethodTableAccessService
from src.knowledge.weaviate_business_store import WeaviateBusinessInterpretStore
from src.knowledge.weaviate_interpretation_store import WeaviateMethodInterpretStore
from src.app.services.weaviate_data_service import WeaviateDataService
from src.app.views.scene_template_room.scene_config_view import SceneTemplateConfigView
from src.app.views.scene_template_room.scene_subcontexts import (
    SceneGraphContext,
    SceneProjectContext,
    SceneVectorsContext,
    capability_nid,
    domain_nid,
    service_nid_from_service_id,
    term_nid,
)


def _safe_json_loads(raw: Any, default: Any) -> Any:
    try:
        if raw is None:
            return default
        if isinstance(raw, (dict, list)):
            return raw
        return json.loads(str(raw))
    except Exception:
        return default


@dataclass(frozen=True)
class SceneTemplateContext:
    """场景样板间根上下文：组合 graph / vectors / project 子域，并保留扁平 API 兼容。

    新代码可优先使用 ``ctx.graph``、``ctx.vectors``、``ctx.project`` 缩小依赖面。
    """

    services: AppServices
    graph: SceneGraphContext
    vectors: SceneVectorsContext
    project: SceneProjectContext

    # --- 扁平访问（兼容既有场景，避免全量改调用方）---

    @property
    def graph_backend(self) -> Any | None:
        return self.graph.graph_backend

    @property
    def neo4j_backend(self) -> Any | None:
        return self.graph.neo4j_backend

    @property
    def weaviate_data_svc(self) -> WeaviateDataService:
        return self.vectors.weaviate_data_svc

    @property
    def repo_cfg(self) -> dict[str, Any]:
        return self.project.repo_cfg

    @property
    def config_view(self) -> SceneTemplateConfigView:
        """只读项目配置视图（域 / knowledge 各段），避免场景内手写 YAML 路径。"""
        return self.project.config_view

    @property
    def code_vector_store(self) -> Any | None:
        return self.vectors.code_vector_store

    @property
    def method_interpret_store(self) -> WeaviateMethodInterpretStore | None:
        return self.vectors.method_interpret_store

    @property
    def business_interpret_store(self) -> WeaviateBusinessInterpretStore | None:
        return self.vectors.business_interpret_store

    @property
    def method_table_access_svc(self) -> MethodTableAccessService | None:
        return self.project.method_table_access_svc

    def get_graph_backend_memory_first(self) -> Any | None:
        return self.graph.get_graph_backend_memory_first()

    def get_graph_backend_topology_primary(self) -> Any | None:
        return self.graph.get_graph_backend_topology_primary()

    def get_graph_backend_topology_merge_secondary(self) -> Any | None:
        return self.graph.get_graph_backend_topology_merge_secondary()

    def get_backend(self) -> Any | None:
        return self.graph.get_backend()

    def get_calls_graph_backend(self) -> Any | None:
        return self.graph.get_calls_graph_backend()

    def has_graph_backend(self) -> bool:
        return self.graph.has_graph_backend()

    def get_node(self, nid: str) -> Optional[dict]:
        return self.graph.get_node(nid)

    def get_code_snippet(self, entity_id: str) -> str:
        return self.vectors.get_code_snippet(entity_id)

    def get_node_name(self, nid: str) -> str:
        if not nid:
            return ""
        n = self.graph.get_node(nid)
        if n and (n.get("name") or "").strip():
            return str(n.get("name")).strip()
        if self.vectors.method_interpret_store is not None:
            try:
                inter = self.vectors.method_interpret_store.get_by_method_id(nid)
                if inter:
                    mn = str(inter.get("method_name") or "").strip()
                    if mn:
                        return mn
            except Exception:
                pass
        return nid

    def method_listing_display(self, method_id: str) -> dict[str, str]:
        mid = (method_id or "").strip()
        title = ""
        signature = ""
        class_name = ""
        n = self.graph.get_node(mid)
        if n:
            title = str(n.get("name") or "").strip()
            signature = str(n.get("signature") or "").strip()
            class_name = str(n.get("class_name") or "").strip()
        inter: dict[str, Any] | None = None
        if self.vectors.method_interpret_store is not None:
            try:
                inter = self.vectors.method_interpret_store.get_by_method_id(mid)
            except Exception:
                inter = None
        if inter:
            if not title:
                title = str(inter.get("method_name") or "").strip()
            if not signature:
                signature = str(inter.get("signature") or "").strip()
            if not class_name:
                class_name = str(inter.get("class_name") or "").strip()
        if not title or title == mid:
            tail = mid.rsplit("//", 1)[-1] if "//" in mid else mid
            title = f"（未解析到方法名）{tail}"
        return {
            "title": title,
            "signature": signature,
            "class_name": class_name,
        }

    def resolve_method_id(
        self, method_input: str, *, module_filter: str | None = None
    ) -> Optional[str]:
        return self.graph.resolve_method_id(method_input, module_filter=module_filter)

    @staticmethod
    def parse_related_entity_ids(related_entity_ids_json: Any) -> list[str]:
        val = _safe_json_loads(related_entity_ids_json, default=[])
        if isinstance(val, list):
            return [str(x) for x in val if x]
        return []

    def capability_nid(self, capability_id: str) -> str:
        return capability_nid(capability_id)

    def domain_nid(self, domain_id: str) -> str:
        return domain_nid(domain_id)

    def term_nid(self, term_id: str) -> str:
        return term_nid(term_id)

    def service_nid_from_service_id(self, service_id: str) -> str:
        return service_nid_from_service_id(service_id)


@st.cache_resource
def _build_code_vector_store(
    *,
    backend: str,
    enabled: bool,
    dimension: int,
    allow_fallback_to_memory: bool,
    weaviate_url: str | None,
    weaviate_grpc_port: int | None,
    collection_name: str | None,
    weaviate_api_key: str | None,
    impl_version: int,
):
    return VectorStoreFactory.create(
        backend=backend,
        enabled=enabled,
        dimension=dimension,
        allow_fallback_to_memory=allow_fallback_to_memory,
        weaviate_url=weaviate_url,
        weaviate_grpc_port=weaviate_grpc_port,
        collection_name=collection_name,
        weaviate_api_key=weaviate_api_key,
    )


@st.cache_resource
def _build_method_interpret_store(
    *,
    weaviate_url: str,
    weaviate_grpc_port: int,
    collection_name: str,
    dimension: int,
    api_key: str | None,
    impl_version: int,
):
    try:
        return WeaviateMethodInterpretStore(
            url=weaviate_url,
            grpc_port=int(weaviate_grpc_port),
            collection_name=collection_name,
            dimension=int(dimension),
            api_key=api_key,
        )
    except Exception:
        return None


@st.cache_resource
def _build_business_interpret_store(
    *,
    weaviate_url: str,
    weaviate_grpc_port: int,
    collection_name: str,
    dimension: int,
    api_key: str | None,
    impl_version: int,
):
    try:
        return WeaviateBusinessInterpretStore(
            url=weaviate_url,
            grpc_port=int(weaviate_grpc_port),
            collection_name=collection_name,
            dimension=int(dimension),
            api_key=api_key,
        )
    except Exception:
        return None


def _load_repo_cfg_and_config_view(services: AppServices) -> tuple[dict[str, Any], SceneTemplateConfigView]:
    """加载 project.yaml：返回 ``model_dump`` 字典（供 cache 键与遗留逻辑）与类型化只读视图。"""
    try:
        loaded = services.load_config_fn(str(services.root / "config/project.yaml"))
    except Exception:
        return {}, SceneTemplateConfigView.empty()

    if isinstance(loaded, ProjectConfig):
        pc = loaded
        repo_cfg = pc.model_dump()
    elif isinstance(loaded, dict):
        pc = ProjectConfig.from_yaml_dict(loaded)
        repo_cfg = pc.model_dump()
    elif callable(getattr(loaded, "model_dump", None)):
        raw = loaded.model_dump()
        repo_cfg = raw if isinstance(raw, dict) else {}
        pc = ProjectConfig.from_yaml_dict(repo_cfg) if repo_cfg else None
        if pc is None:
            return {}, SceneTemplateConfigView.empty()
        return repo_cfg, SceneTemplateConfigView.from_project_config(pc)
    else:
        return {}, SceneTemplateConfigView.empty()

    return repo_cfg, SceneTemplateConfigView.from_project_config(pc)


def _build_method_table_access_svc(repo_cfg: dict[str, Any]) -> MethodTableAccessService | None:
    from pathlib import Path

    repo = repo_cfg.get("repo") or {}
    schema = repo_cfg.get("schema") or {}
    repo_path = repo.get("path")
    if not repo_path:
        return None
    root = Path(str(repo_path))
    if not root.exists():
        return None
    ddl_path = str(schema.get("ddl_path") or "document/sql/mall.sql")
    mapper_glob = str(schema.get("mapper_glob") or "**/mapper/*Mapper.xml")
    return MethodTableAccessService(repo_root=root, ddl_path=ddl_path, mapper_glob=mapper_glob)


def build_scene_template_context(
    *,
    services: AppServices,
    graph_backend: Any | None,
    neo4j_backend: Any | None,
) -> SceneTemplateContext:
    """构造场景样板间上下文。向量库等初始化通过 cache_resource 复用。"""
    repo_cfg, config_view = _load_repo_cfg_and_config_view(services)

    wsvc = services.weaviate_data_svc
    vcode = config_view.knowledge.vectordb_code
    code_vector_store = _build_code_vector_store(
        backend=str(vcode.backend or "weaviate"),
        enabled=bool(vcode.enabled),
        dimension=int(vcode.dimension or 1024),
        allow_fallback_to_memory=bool(vcode.allow_fallback_to_memory),
        weaviate_url=vcode.weaviate_url or None,
        weaviate_grpc_port=int(vcode.weaviate_grpc_port),
        collection_name=vcode.collection_name or None,
        weaviate_api_key=vcode.weaviate_api_key,
        impl_version=2,
    )

    vi = config_view.knowledge.vectordb_interpret
    method_interpret_store = None
    if vi.enabled and str(vi.backend or "weaviate").strip().lower() == "weaviate":
        method_interpret_store = _build_method_interpret_store(
            weaviate_url=str(vi.weaviate_url or DEFAULT_WEAVIATE_HTTP_URL),
            weaviate_grpc_port=int(vi.weaviate_grpc_port or DEFAULT_WEAVIATE_GRPC_PORT),
            collection_name=str(vi.collection_name or DEFAULT_COLLECTION_METHOD_INTERPRETATION),
            dimension=int(vi.dimension or 1024),
            api_key=vi.weaviate_api_key,
            impl_version=1,
        )

    vb = config_view.knowledge.vectordb_business
    business_interpret_store = None
    if vb.enabled and str(vb.backend or "weaviate").strip().lower() == "weaviate":
        business_interpret_store = _build_business_interpret_store(
            weaviate_url=str(vb.weaviate_url or DEFAULT_WEAVIATE_HTTP_URL),
            weaviate_grpc_port=int(vb.weaviate_grpc_port or DEFAULT_WEAVIATE_GRPC_PORT),
            collection_name=str(vb.collection_name or DEFAULT_COLLECTION_BUSINESS_INTERPRETATION),
            dimension=int(vb.dimension or 1024),
            api_key=vb.weaviate_api_key,
            impl_version=1,
        )

    graph = SceneGraphContext(graph_backend=graph_backend, neo4j_backend=neo4j_backend)
    vectors = SceneVectorsContext(
        weaviate_data_svc=wsvc,
        code_vector_store=code_vector_store,
        method_interpret_store=method_interpret_store,
        business_interpret_store=business_interpret_store,
    )
    project = SceneProjectContext(
        repo_cfg=repo_cfg,
        config_view=config_view,
        method_table_access_svc=_build_method_table_access_svc(repo_cfg),
    )

    return SceneTemplateContext(
        services=services,
        graph=graph,
        vectors=vectors,
        project=project,
    )
