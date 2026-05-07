"""服务层 REST API：检索、影响分析、图谱子图。

路由优先通过 ``Depends(get_app_context)`` 注入 `AppContext`，便于测试覆盖与逐步摆脱隐式单例。
``set_global_graph`` / ``set_global_config`` 仍保留，委托给 ``AppContext.get()``。
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from src.core.context import AppContext, get_app_context
from src.knowledge import KnowledgeGraph
from src.service.auth_dependencies import get_current_user
from src.service.auth_models import User
from src.service.auth_router import router as auth_router
from src.service.project_router import router as project_router
from src.service.qa_router import router as qa_router

# load_dotenv 让 KE_JWT_SECRET / KE_DB_URL 等从 .env / .env.local 加载
try:
    from pathlib import Path
    from dotenv import load_dotenv
    # 优先读 .env.local（开发覆盖），再读 .env
    _root = Path(__file__).resolve().parents[2]
    load_dotenv(_root / ".env.local", override=False)
    load_dotenv(_root / ".env", override=False)
except ImportError:
    pass


def set_global_graph(g: KnowledgeGraph) -> None:
    """由流水线在构建后调用；委托给 ``AppContext`` 单例（兼容旧代码）。"""
    get_app_context().set_graph(g)


def set_global_config(cfg: dict) -> None:
    """由流水线在构建后调用；委托给 ``AppContext`` 单例（兼容旧代码）。"""
    get_app_context().set_config(cfg)


def get_global_config() -> Optional[dict]:
    return get_app_context().get_config()


def _graph_http(ctx: AppContext) -> KnowledgeGraph:
    try:
        return ctx.get_graph()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


def get_graph() -> KnowledgeGraph:
    """未注入 context 时使用默认单例（脚本/旧调用）。"""
    return _graph_http(get_app_context())


def get_graph_optional() -> Optional[KnowledgeGraph]:
    """供 Streamlit 等前端使用：返回图实例或 None（未构建时）。"""
    return get_app_context().get_graph_optional()


app = FastAPI(
    title="代码知识工程 API",
    description="检索、影响分析、图谱可视化数据",
    version="0.1.0",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(auth_router)
app.include_router(project_router)
app.include_router(qa_router)


@app.on_event("startup")
async def init_qa_engine() -> None:
    """启动时初始化 QA 引擎（注入到 app.state）。

    MVP 阶段（W6 第 1 步）：
      - retriever  使用 StubRetriever（不查 Weaviate / Graph，返回空 context）
      - synthesizer 使用 DashScopeProvider（通义千问 OpenAI 兼容接口）

    W6 第 2 步会替换 StubRetriever → 真实 QARetriever（接主仓 Weaviate + Graph）。
    """
    if hasattr(app.state, "qa_retriever") and app.state.qa_retriever is not None:
        return  # 测试已经手动注入

    from src.service.qa_engine import QASynthesizer
    from src.service.qa_engine.stub_retriever import StubRetriever
    from src.service.qa_engine.llm_dashscope import DashScopeProvider

    try:
        llm = DashScopeProvider()
        app.state.qa_retriever = StubRetriever()
        app.state.qa_synthesizer = QASynthesizer(llm_provider=llm)
        print(f"[startup] qa_engine ready (model={llm.model})")
    except Exception as e:
        print(f"[startup] qa_engine init failed: {e}")
        app.state.qa_retriever = None
        app.state.qa_synthesizer = None


@app.get("/health")
def health(ctx: AppContext = Depends(get_app_context)) -> dict:
    return {"status": "ok", "graph_loaded": ctx.get_graph_optional() is not None}


@app.get("/search")
def search(
    ctx: AppContext = Depends(get_app_context),
    _user: User = Depends(get_current_user),
    q: str = Query(..., description="名称或关键词"),
    entity_type: Optional[str] = Query(None, description="筛选实体类型: class, method, Service, BusinessDomain 等"),
    mode: str = Query("name", description="name=按名称模糊检索, semantic=按语义相似检索（需启用向量库）"),
    top_k: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """检索：按名称或按语义相似度。"""
    g = _graph_http(ctx)
    if mode == "semantic" and getattr(g, "_vector_store", None) and g._vector_store.size() > 0:
        hits = g.similarity_search(q, top_k=top_k)
        return {"query": q, "mode": "semantic", "count": len(hits), "results": hits}
    types = [entity_type] if entity_type else None
    hits = g.search_by_name(q, entity_types=types)
    return {"query": q, "mode": "name", "count": len(hits), "results": hits[:top_k]}


@app.get("/impact")
def impact(
    ctx: AppContext = Depends(get_app_context),
    _user: User = Depends(get_current_user),
    entity_id: str = Query(..., description="实体 ID，如 class://xxx 或 method://xxx"),
    direction: str = Query("down", description="down=被谁调用/依赖, up=依赖了谁"),
    max_depth: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    """影响分析：返回从该实体出发的依赖/被依赖闭包。"""
    g = _graph_http(ctx)
    if not g._g.has_node(entity_id):
        raise HTTPException(status_code=404, detail=f"实体不存在: {entity_id}")
    closure = g.impact_closure(entity_id, direction=direction, max_depth=max_depth)
    nodes = [g.get_node(nid) for nid in closure if g.get_node(nid)]
    return {"entity_id": entity_id, "direction": direction, "count": len(closure), "nodes": nodes}


@app.get("/subgraph/service/{service_id}")
def subgraph_service(
    service_id: str,
    ctx: AppContext = Depends(get_app_context),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """按服务/模块获取子图（用于前端图谱可视化）。"""
    g = _graph_http(ctx)
    return g.subgraph_for_service(service_id)


@app.get("/stats")
def stats(
    ctx: AppContext = Depends(get_app_context),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """图谱统计。"""
    g = _graph_http(ctx)
    out = {"nodes": g.node_count(), "edges": g.edge_count()}
    if getattr(g, "_vector_store", None):
        out["vector_store_size"] = g._vector_store.size()
    if getattr(g, "version", None):
        out["version"] = g.version
    return out


def _load_config_for_neo4j(ctx: AppContext) -> Optional[dict]:
    """获取用于 Neo4j 的配置（来自 context 或从 project.yaml 加载）。"""
    cfg = ctx.get_config()
    if cfg:
        return cfg
    from pathlib import Path

    from src.pipeline.config_bootstrap import load_config

    # Streamlit 常见 cwd 与仓库根不一致；除 cwd 外再尝试本包所在项目根下的默认配置
    here = Path(__file__).resolve()
    candidates = [
        Path.cwd() / "config" / "project.yaml",
        here.parents[2] / "config" / "project.yaml",
    ]
    for default_path in candidates:
        if default_path.is_file():
            proj = load_config(default_path)
            return proj.model_dump()
    return None


def _get_neo4j_calls_backend(ctx: AppContext):
    """从 context 关联配置创建 Neo4j 连接，供 /calls/* 使用；仅走 Neo4j，不走内存图。"""
    cfg = _load_config_for_neo4j(ctx)
    if not cfg:
        raise HTTPException(
            status_code=503,
            detail="未找到配置（请先运行流水线或保证 config/project.yaml 存在），CALLS 查询需 Neo4j 配置",
        )
    gc = (cfg.get("knowledge") or {}).get("graph") or {}
    from src.knowledge.factories import GraphBackendFactory

    return GraphBackendFactory.create(
        "neo4j",
        neo4j_uri=gc.get("neo4j_uri") or "bolt://localhost:7687",
        neo4j_user=gc.get("neo4j_user") or "neo4j",
        neo4j_password=gc.get("neo4j_password") or "password",
        neo4j_database=gc.get("neo4j_database") or "neo4j",
    )


def get_neo4j_backend_optional():
    """
    供 Streamlit 等使用：若已配置 Neo4j 则返回 Neo4jGraphBackend 实例，否则返回 None。
    调用方负责在不再使用时调用 backend.close()。
    """
    ctx = get_app_context()
    cfg = _load_config_for_neo4j(ctx)
    if not cfg:
        return None
    gc = (cfg.get("knowledge") or {}).get("graph") or {}
    if gc.get("backend") != "neo4j":
        return None
    from src.knowledge.factories import GraphBackendFactory

    try:
        return GraphBackendFactory.create(
            "neo4j",
            neo4j_uri=gc.get("neo4j_uri") or "bolt://localhost:7687",
            neo4j_user=gc.get("neo4j_user") or "neo4j",
            neo4j_password=gc.get("neo4j_password") or "password",
            neo4j_database=gc.get("neo4j_database") or "neo4j",
        )
    except Exception:
        return None


@app.get("/calls/callees")
def get_callees(
    ctx: AppContext = Depends(get_app_context),
    _user: User = Depends(get_current_user),
    class_name: str = Query(..., description="类名"),
    method_name: str = Query(..., description="方法名"),
) -> dict[str, Any]:
    """给定类名+方法名，从 Neo4j 查询该方法直接调用的其他方法列表。每项含 class_name、method_name。"""
    backend = _get_neo4j_calls_backend(ctx)
    try:
        items = backend.query_direct_callees(class_name.strip(), method_name.strip())
        return {"class_name": class_name, "method_name": method_name, "count": len(items), "callees": items}
    finally:
        backend.close()


@app.get("/calls/callers")
def get_callers(
    ctx: AppContext = Depends(get_app_context),
    _user: User = Depends(get_current_user),
    class_name: str = Query(..., description="类名"),
    method_name: str = Query(..., description="方法名"),
) -> dict[str, Any]:
    """给定类名+方法名，从 Neo4j 查询所有直接调用该方法的其他方法列表。每项含 class_name、method_name。"""
    backend = _get_neo4j_calls_backend(ctx)
    try:
        items = backend.query_direct_callers(class_name.strip(), method_name.strip())
        return {"class_name": class_name, "method_name": method_name, "count": len(items), "callers": items}
    finally:
        backend.close()


@app.post("/knowledge/ontology/run")
def run_ontology(
    ctx: AppContext = Depends(get_app_context),
    _user: User = Depends(get_current_user),
    export_owl: bool = Query(True, description="是否导出 OWL"),
    reasoner: str = Query("builtin", description="builtin=传递闭包, hermit=需 Java+HermiT"),
    write_inferred_to_graph: bool = Query(True, description="是否将推理边写回图"),
) -> dict[str, Any]:
    """
    按需执行 OWL 本体流水线：导出 OWL、运行推理、可选写回图。
    需先运行流水线构建知识图谱；需安装 pip install -e '.[owl]'。
    """
    g = _graph_http(ctx)
    try:
        from src.knowledge.ontology import run_ontology_pipeline
    except ImportError as e:
        raise HTTPException(
            status_code=503,
            detail=f"未安装 OWL 依赖，请执行: pip install -e '.[owl]'；{e!r}",
        ) from e
    result = run_ontology_pipeline(
        g,
        export_owl=export_owl,
        export_path=None,
        run_reasoner=reasoner,
        write_inferred_to_graph=write_inferred_to_graph,
    )
    return result


@app.post("/knowledge/load_snapshot")
def load_snapshot(
    ctx: AppContext = Depends(get_app_context),
    _user: User = Depends(get_current_user),
    snapshot_dir: str = Query(..., description="快照目录路径"),
) -> dict[str, Any]:
    """从磁盘加载知识图谱快照，替换当前图。"""
    from pathlib import Path

    g = _graph_http(ctx)
    path = Path(snapshot_dir)
    if not path.is_dir() or not (path / "graph.json").exists():
        raise HTTPException(status_code=400, detail="快照目录无效或缺少 graph.json")
    from src.persistence.repositories import GraphSnapshotRepository

    GraphSnapshotRepository().load(g, path)
    return {"message": "快照已加载", "nodes": g.node_count(), "edges": g.edge_count(), "version": g.version}


@app.get("/qa")
def qa(
    ctx: AppContext = Depends(get_app_context),
    _user: User = Depends(get_current_user),
    q: str = Query(..., description="自然语言问题"),
    top_k: int = Query(10, ge=1, le=50, description="返回检索条数"),
) -> dict[str, Any]:
    """
    问答：基于图谱检索返回相关实体与关系，作为结构化答案。
    可选后续对接大模型做自然语言生成，当前为「检索即答案」。
    """
    g = _graph_http(ctx)
    hits = g.search_by_name(q, entity_types=None)[:top_k]
    related: list[dict] = []
    for h in hits:
        nid = h.get("id")
        if not nid:
            continue
        succ = g.successors(nid, rel_type=None)
        pred = g.predecessors(nid, rel_type=None)
        related.append({
            "entity": h,
            "successors": [g.get_node(s) for s in succ[:5] if g.get_node(s)],
            "predecessors": [g.get_node(p) for p in pred[:5] if g.get_node(p)],
        })
    return {
        "question": q,
        "answer_type": "retrieval",
        "count": len(related),
        "results": related,
        "message": "基于图谱检索；可对接大模型生成自然语言回答",
    }


def _doc_service_body(g: KnowledgeGraph, service_id: str) -> dict[str, Any]:
    sid = service_id if service_id.startswith("service://") else f"service://{service_id}"
    if not g._g.has_node(sid):
        raise HTTPException(status_code=404, detail=f"服务不存在: {sid}")
    node = g.get_node(sid)
    sub = g.subgraph_for_service(service_id)
    domains = g.successors(sid, rel_type="BELONGS_TO_DOMAIN")
    domain_names = [g.get_node(d) or {} for d in domains]
    return {
        "service_id": sid,
        "name": node.get("name", sid),
        "entity_type": "Service",
        "summary": f"服务 {node.get('name', sid)}：共 {len(sub.get('nodes', []))} 个节点，{len(sub.get('edges', []))} 条边。",
        "business_domains": domain_names,
        "subgraph_nodes_count": len(sub.get("nodes", [])),
        "subgraph_edges_count": len(sub.get("edges", [])),
    }


@app.get("/doc/service/{service_id}")
def doc_service(
    service_id: str,
    ctx: AppContext = Depends(get_app_context),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """生成单个服务/模块的说明文档（名称、包含的类/方法数、关联业务域）。"""
    return _doc_service_body(_graph_http(ctx), service_id)


def _doc_domain_body(g: KnowledgeGraph, domain_id: str) -> dict[str, Any]:
    did = domain_id if domain_id.startswith("domain://") else f"domain://{domain_id}"
    if not g._g.has_node(did):
        raise HTTPException(status_code=404, detail=f"业务域不存在: {did}")
    node = g.get_node(did)
    capabilities = g.successors(did, rel_type="CONTAINS_CAPABILITY")
    in_domain_entities = g.predecessors(did, rel_type="IN_DOMAIN")
    services = []
    for (u, v, k) in g._g.in_edges(did, keys=True):
        if g._g.edges[u, v, k].get("rel_type") == "BELONGS_TO_DOMAIN" and str(u).startswith("service://"):
            services.append(u)
    return {
        "domain_id": did,
        "name": node.get("name", did),
        "entity_type": "BusinessDomain",
        "summary": f"业务域 {node.get('name', did)}：{len(capabilities)} 个能力，{len(in_domain_entities)} 个代码实体归属。",
        "capability_ids": capabilities,
        "code_entities_count": len(in_domain_entities),
        "services_bearing": [g.get_node(s) for s in services if g.get_node(s)],
    }


@app.get("/doc/domain/{domain_id}")
def doc_domain(
    domain_id: str,
    ctx: AppContext = Depends(get_app_context),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """生成单个业务域的说明文档（名称、关联能力与术语、涉及的服务）。"""
    return _doc_domain_body(_graph_http(ctx), domain_id)


@app.get("/doc/generate")
def doc_generate(
    ctx: AppContext = Depends(get_app_context),
    _user: User = Depends(get_current_user),
    scope: str = Query("all", description="all | service | domain"),
) -> dict[str, Any]:
    """生成模块/服务/业务域级别的文档列表（用于批量导出）。"""
    g = _graph_http(ctx)
    services = [n for n in g._g.nodes if str(n).startswith("service://")]
    domains = [n for n in g._g.nodes if (g._g.nodes[n].get("entity_type") or "").lower() == "businessdomain"]
    out: list[dict] = []
    if scope in ("all", "service"):
        for sid in services:
            try:
                out.append(_doc_service_body(g, sid.replace("service://", "")))
            except Exception:
                pass
    if scope in ("all", "domain"):
        for did in domains:
            try:
                out.append(_doc_domain_body(g, did.replace("domain://", "")))
            except Exception:
                pass
    return {"scope": scope, "count": len(out), "documents": out}
