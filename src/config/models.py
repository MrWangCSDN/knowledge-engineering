"""强类型配置模型：Pydantic 定义，替代 config.get('xxx') or {}。"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from src.core.weaviate_defaults import (
    DEFAULT_COLLECTION_CODE_ENTITY,
    DEFAULT_COLLECTION_TOPOLOGICAL_INTERPRETATION,
    DEFAULT_WEAVIATE_GRPC_PORT,
    DEFAULT_WEAVIATE_HTTP_URL,
)


class RepoModuleConfig(BaseModel):
    """repo.modules 单项。"""
    id: str = ""
    business_domains: list[str] = Field(default_factory=list)


class RepoConfig(BaseModel):
    """repo 配置。"""
    path: str = ""
    version: Optional[str] = None
    language: Optional[str] = None
    modules: list[RepoModuleConfig | dict[str, Any]] = Field(default_factory=list)
    # v2.0：多租户 project_id，写入 Weaviate tenant 与 Neo4j 节点属性。
    # 未配置时 pipeline 会从 config.repo.project_id 或 run_pipeline(project_id=...) 中取，再 fallback "default"。
    project_id: Optional[str] = None


class StructureConfig(BaseModel):
    """structure 配置。"""
    extract_cross_service: bool = True
    java_source_extensions: list[str] = Field(default_factory=lambda: [".java"])


class PipelineConfig(BaseModel):
    """knowledge.pipeline 配置。"""
    include_method_interpretation_build: bool = False
    include_business_interpretation_build: bool = False


class SemanticEmbeddingConfig(BaseModel):
    """knowledge.semantic_embedding 配置。"""
    backend: str = "ollama"
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "bge-m3"


class GraphConfig(BaseModel):
    """knowledge.graph 配置。"""
    backend: str = "memory"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "password"
    neo4j_database: str = "neo4j"


class VectorDBConfig(BaseModel):
    """vectordb-code / vectordb-interpret / vectordb-business 配置。"""
    enabled: bool = True
    backend: str = "weaviate"
    dimension: int = 1024
    weaviate_url: str = DEFAULT_WEAVIATE_HTTP_URL
    weaviate_grpc_port: int = DEFAULT_WEAVIATE_GRPC_PORT
    weaviate_api_key: Optional[str] = None
    collection_name: str = DEFAULT_COLLECTION_CODE_ENTITY
    # Weaviate 创建失败时是否回退内存向量库（默认否，避免误以为已写入 Weaviate）
    allow_fallback_to_memory: bool = False


class TopologicalInterpretationConfig(BaseModel):
    """knowledge.topological_interpretation 配置（原 method_interpretation）。"""
    enabled: bool = False
    language: str = "zh"
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:32b"
    timeout_seconds: int = 120
    max_methods: int = 0
    max_workers: int = 4  # LLM 并发调用数（Ollama 建议 2-4，云端 API 可设 8-20）
    # ollama | openai | anthropic | multi（多LLM负载均衡）
    llm_backend: str = "ollama"
    # --- OpenAI 及兼容 API（llm_backend: openai）---
    openai_api_key: Optional[str] = None
    openai_base_url: Optional[str] = None
    openai_model: str = "gpt-4o-mini"
    openai_max_tokens: Optional[int] = None
    # --- Anthropic（llm_backend: anthropic）---
    anthropic_api_key: Optional[str] = None
    anthropic_model: str = "claude-3-5-sonnet-20241022"
    anthropic_max_tokens: int = 8192
    # openai/anthropic 缺少 Python 依赖时是否回退 Ollama（默认否）
    llm_allow_fallback_to_ollama: bool = False
    # --- Multi Provider（llm_backend: multi）--- 多LLM轮询负载均衡
    multi_providers: Optional[list[dict]] = None


class SnapshotConfig(BaseModel):
    """knowledge.snapshot 配置。"""
    save_after_build: bool = False


class KnowledgeConfig(BaseModel):
    """knowledge 配置。"""
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    semantic_embedding: SemanticEmbeddingConfig = Field(default_factory=SemanticEmbeddingConfig)
    graph: GraphConfig = Field(default_factory=GraphConfig)
    vectordb_code: VectorDBConfig = Field(
        default_factory=lambda: VectorDBConfig(collection_name=DEFAULT_COLLECTION_CODE_ENTITY)
    )
    vectordb_interpret: VectorDBConfig = Field(
        default_factory=lambda: VectorDBConfig(collection_name=DEFAULT_COLLECTION_TOPOLOGICAL_INTERPRETATION)
    )
    topological_interpretation: TopologicalInterpretationConfig = Field(default_factory=TopologicalInterpretationConfig)
    snapshot: SnapshotConfig = Field(default_factory=SnapshotConfig)

    # Pydantic v2 弃用 class-based Config，改为 model_config
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    @classmethod
    def from_raw(cls, raw: dict[str, Any] | None) -> "KnowledgeConfig":
        """从 YAML 原始 dict 解析（键名带连字符）。"""
        if not raw:
            return cls()
        r = raw.copy()
        pipe = PipelineConfig.model_validate(r.get("pipeline") or {})
        sem = SemanticEmbeddingConfig.model_validate(r.get("semantic_embedding") or {})
        graph = GraphConfig.model_validate(r.get("graph") or {})
        vcode = VectorDBConfig.model_validate(r.get("vectordb-code") or {})
        vinterp = VectorDBConfig.model_validate(r.get("vectordb-interpret") or {})
        ti = TopologicalInterpretationConfig.model_validate(
            r.get("topological_interpretation") or r.get("method_interpretation") or {}
        )
        snap = SnapshotConfig.model_validate(r.get("snapshot") or {})
        return cls(
            pipeline=pipe,
            semantic_embedding=sem,
            graph=graph,
            vectordb_code=vcode,
            vectordb_interpret=vinterp,
            topological_interpretation=ti,
            snapshot=snap,
        )

    def to_interpret_dict(self) -> dict[str, Any]:
        """导出 topological_interpretation 为 dict（如 ``ProjectConfig.model_dump``、外部脚本）。"""
        m = self.topological_interpretation
        return {
            "enabled": m.enabled,
            "language": m.language,
            "ollama_base_url": m.ollama_base_url,
            "ollama_model": m.ollama_model,
            "timeout_seconds": m.timeout_seconds,
            "max_methods": m.max_methods,
            "llm_backend": m.llm_backend,
            "openai_api_key": m.openai_api_key,
            "openai_base_url": m.openai_base_url,
            "openai_model": m.openai_model,
            "openai_max_tokens": m.openai_max_tokens,
            "anthropic_api_key": m.anthropic_api_key,
            "anthropic_model": m.anthropic_model,
            "anthropic_max_tokens": m.anthropic_max_tokens,
            "llm_allow_fallback_to_ollama": m.llm_allow_fallback_to_ollama,
        }

    def to_vectordb_interpret_dict(self) -> dict[str, Any]:
        """导出 vectordb-interpret 为 dict；流水线内优先使用 ``self.vectordb_interpret`` 对象。"""
        v = self.vectordb_interpret
        return {
            "enabled": v.enabled,
            "backend": v.backend,
            "dimension": v.dimension,
            "weaviate_url": v.weaviate_url,
            "weaviate_grpc_port": v.weaviate_grpc_port,
            "weaviate_api_key": v.weaviate_api_key,
            "collection_name": v.collection_name,
            "allow_fallback_to_memory": v.allow_fallback_to_memory,
        }

    def to_vectordb_code_dict(self) -> dict[str, Any]:
        """供 vectordb-code 使用的 dict。"""
        v = self.vectordb_code
        return {
            "enabled": v.enabled,
            "backend": v.backend,
            "dimension": v.dimension,
            "weaviate_url": v.weaviate_url,
            "weaviate_grpc_port": v.weaviate_grpc_port,
            "weaviate_api_key": v.weaviate_api_key,
            "collection_name": v.collection_name,
            "allow_fallback_to_memory": v.allow_fallback_to_memory,
        }

    def to_graph_dict(self, project_id: Optional[str] = None) -> dict[str, Any]:
        """供 graph 使用的 dict。

        v2.0：project_id 可选；若传入则写入 dict，供 _sync_graph_to_neo4j / GraphBackendFactory 透传。
        """
        g = self.graph
        d: dict[str, Any] = {
            "backend": g.backend,
            "neo4j_uri": g.neo4j_uri,
            "neo4j_user": g.neo4j_user,
            "neo4j_password": g.neo4j_password,
            "neo4j_database": g.neo4j_database,
        }
        if project_id:
            d["project_id"] = project_id
        return d

    def to_snapshot_dict(self) -> dict[str, Any]:
        """供 snapshot 使用的 dict。"""
        s = self.snapshot
        return {"save_after_build": s.save_after_build}


class ServiceConfig(BaseModel):
    """service 配置。"""
    host: str = "0.0.0.0"
    port: int = 8000


class ProjectConfig(BaseModel):
    """完整项目配置。"""

    repo: RepoConfig = Field(default_factory=RepoConfig)
    domain: dict[str, Any] = Field(default_factory=dict)
    structure: StructureConfig = Field(default_factory=StructureConfig)
    # 方法↔表、DDL 等（YAML 顶层键名仍为 ``schema``，避免与 Pydantic 保留名冲突）
    table_access_schema: dict[str, Any] = Field(default_factory=dict)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    service: ServiceConfig = Field(default_factory=ServiceConfig)

    # Pydantic v2 弃用 class-based Config，改为 model_config
    model_config = ConfigDict(extra="allow")

    @classmethod
    def from_yaml_dict(cls, raw: dict[str, Any] | None) -> "ProjectConfig":
        """从 load_config 返回的 YAML dict 解析。"""
        if not raw:
            return cls()
        repo = RepoConfig.model_validate(raw.get("repo") or {})
        struct = StructureConfig.model_validate(raw.get("structure") or {})
        knowledge = KnowledgeConfig.from_raw(raw.get("knowledge"))
        service = ServiceConfig.model_validate(raw.get("service") or {})
        return cls(
            repo=repo,
            domain=raw.get("domain") or {},
            structure=struct,
            table_access_schema=raw.get("schema") or {},
            knowledge=knowledge,
            service=service,
        )

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        """导出为 dict；流水线会写入 ``AppContext.set_config``（或自定义 ``app_context``）。"""
        return {
            "repo": self.repo.model_dump(),
            "domain": self.domain,
            "structure": self.structure.model_dump(),
            "schema": self.table_access_schema,
            "knowledge": {
                "pipeline": self.knowledge.pipeline.model_dump(),
                "semantic_embedding": self.knowledge.semantic_embedding.model_dump(),
                "graph": self.knowledge.graph.model_dump(),
                "vectordb-code": self.knowledge.to_vectordb_code_dict(),
                "vectordb-interpret": self.knowledge.to_vectordb_interpret_dict(),
                "topological_interpretation": self.knowledge.to_interpret_dict(),
                "snapshot": self.knowledge.to_snapshot_dict(),
            },
            "service": self.service.model_dump(),
        }
