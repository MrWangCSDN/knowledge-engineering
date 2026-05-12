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
from src.service.admin_router import router as admin_router
from src.service.auth_router import router as auth_router
from src.service.credentials_router import router as credentials_router  # v2.0 user-scoped 凭证路由
from src.service.group_router import router as group_router              # v2.0 Groups CRUD 路由
from src.service.project_member_router import router as project_member_router  # v2.0 Project Members CRUD 路由
from src.service.user_router import router as user_router                      # v2.0 User Management CRUD 路由
from src.service.project_router import router as project_router
from src.service.qa_router import router as qa_router
from src.service.audit_router import router as audit_router  # v2.0 Task 11：审计日志查询路由

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
app.include_router(admin_router)
app.include_router(credentials_router)  # v2.0：用户级凭证 CRUD（/credentials/*）
app.include_router(group_router)        # v2.0：Groups CRUD（/groups/*）
app.include_router(project_member_router)  # v2.0：Project Members CRUD（/projects/{pid}/members/*）
app.include_router(user_router)            # v2.0：User Management CRUD（/admin/users/*）
app.include_router(audit_router)           # v2.0 Task 11：审计日志查询（/admin/audit-logs + /groups/{gid}/audit-logs）


@app.on_event("startup")
async def init_qa_engine() -> None:
    """启动时初始化 QA 引擎（注入到 app.state）。

    v1.1 起：
      - retriever 优先用真实 QARetriever（Weaviate BusinessInterpretation + Neo4j）
      - 任何后端不可用 → 自动退回 StubRetriever（不让 chat 端到端崩）
      - synthesizer 仍用 DashScopeProvider（通义千问 OpenAI 兼容接口）

    环境变量（写在 .env.local）：
      WEAVIATE_URL         默认 http://localhost:8080
      WEAVIATE_GRPC_PORT   默认 50051
      WEAVIATE_API_KEY     （可选，私有 Weaviate 需要）
      NEO4J_URI            默认 bolt://localhost:7687
      NEO4J_USER           默认 neo4j
      NEO4J_PASSWORD       必填（生产用）；缺失时只走 StubRetriever
    """
    # hasattr 判断属性是否存在；测试场景里测试夹具可能预先注入了 qa_retriever
    if hasattr(app.state, "qa_retriever") and app.state.qa_retriever is not None:
        return  # 测试已经手动注入，不要覆盖

    # os：标准库，读环境变量
    import os
    # 标准日志：startup 信息走 logger，方便生产线上 collected logs 看
    import logging

    from src.service.qa_engine import QASynthesizer
    from src.service.qa_engine.llm_dashscope import DashScopeProvider
    from src.service.qa_engine.router import SkillRouter

    _log = logging.getLogger("qa_engine.startup")

    # 先初始化 LLM —— 这是 chat 的"必备"组件，挂了就别拉 retriever 了
    try:
        llm = DashScopeProvider()
    except Exception as e:
        # `from None` 是 Python 异常链的"切断"语法；这里直接打日志不抛出
        _log.error("[startup] DashScope LLM 初始化失败: %s", e)
        app.state.qa_retriever = None
        app.state.qa_synthesizer = None
        app.state.qa_router = None
        return

    # 试图构造真实 QARetriever
    real_retriever, neo4j_adapter_for_shutdown = _try_build_real_retriever()

    # 真实 retriever 没构造成功 → 退回 Stub（chat 仍能跑，只是 LLM 看不到 context）
    if real_retriever is None:
        from src.service.qa_engine.stub_retriever import StubRetriever
        app.state.qa_retriever = StubRetriever()
        app.state.qa_tools = None
        _log.warning("[startup] 真实 retriever 未就绪 → 使用 StubRetriever（context 将为空）")
    else:
        app.state.qa_retriever = real_retriever
        # 保留 adapter 引用，shutdown 时关 Neo4j driver
        app.state.qa_neo4j_adapter = neo4j_adapter_for_shutdown
        # v1.2 MCP 工具集；取自 _try_build_real_retriever 里临时挂的 hint
        app.state.qa_tools = getattr(real_retriever, "_tool_registry_hint", None)
        _log.info(
            "[startup] 真实 QARetriever 就绪（Weaviate + Neo4j），qa_tools=%s",
            len(app.state.qa_tools.list_tools()) if app.state.qa_tools else 0,
        )

    # v1.3：根据环境变量 KE_QA_USE_REACT 决定用 QASynthesizer 还是 ReActSynthesizer
    # 默认关闭（保留 v1.2 的稳定路径）；设 KE_QA_USE_REACT=1 才启用 ReAct 循环
    use_react = os.environ.get("KE_QA_USE_REACT", "").strip() in {"1", "true", "yes"}
    if use_react and app.state.qa_tools is not None:
        from src.service.qa_engine.react_synthesizer import ReActSynthesizer
        max_iter = int(os.environ.get("KE_QA_REACT_MAX_ITER", "3"))
        app.state.qa_synthesizer = ReActSynthesizer(
            llm_provider=llm,
            tool_registry=app.state.qa_tools,
            max_iterations=max_iter,
        )
        _log.info(
            "[startup] qa_engine ready (model=%s, mode=ReAct, max_iter=%d, tools=%d)",
            llm.model, max_iter, len(app.state.qa_tools.list_tools()),
        )
    else:
        app.state.qa_synthesizer = QASynthesizer(llm_provider=llm)
        _log.info("[startup] qa_engine ready (model=%s, mode=QA-single-shot)", llm.model)

    # SkillRouter：纯关键词路径无依赖；LLM provider 注入后 route_async 才有 fallback 能力
    # 当前 SSE 链路只调同步 router.route()，所以连 None 也能跑，
    # 但既然 LLM 已经在手就一并注入，未来 route_async 想用立刻可用。
    app.state.qa_router = SkillRouter(llm_provider=llm)


def _try_build_real_retriever() -> tuple[Any, Any]:
    """构造真实 QARetriever；任意后端失败就返回 (None, None)。

    分两步：
      1. 连 Weaviate BusinessInterpretation
      2. 连 Neo4j

    任一步失败都视作整体失败 → 退回 StubRetriever（避免半连接状态）。

    :return: (QARetriever 或 None, Neo4jGraphAdapter 或 None)
    """
    import os
    import logging

    _log = logging.getLogger("qa_engine.startup")

    # ─── 1) Weaviate ───
    try:
        # 主仓的业务解读 store；直接用它的默认参数 + .env 覆盖 URL/Key
        from src.knowledge.weaviate_business_store import WeaviateBusinessInterpretStore
        from src.service.qa_engine.adapters import WeaviateBusinessAdapter

        weaviate_url = os.environ.get("WEAVIATE_URL", "http://localhost:8080")
        weaviate_grpc_port = int(os.environ.get("WEAVIATE_GRPC_PORT", "50051"))
        weaviate_api_key = os.environ.get("WEAVIATE_API_KEY") or None
        # 维度固定 1024（bge-m3 输出维度）；如果将来换 embedding 模型再环境变量化
        weaviate_dimension = int(os.environ.get("WEAVIATE_DIMENSION", "1024"))

        biz_store = WeaviateBusinessInterpretStore(
            url=weaviate_url,
            grpc_port=weaviate_grpc_port,
            dimension=weaviate_dimension,
            api_key=weaviate_api_key,
        )
        biz_adapter = WeaviateBusinessAdapter(biz_store)
        _log.info("[startup] Weaviate 业务解读 store 连接成功: %s", weaviate_url)
    except Exception as e:
        _log.warning("[startup] Weaviate 业务解读连接失败: %s", e)
        return None, None

    # ─── 2) Neo4j ───
    try:
        from src.knowledge.graph_neo4j import Neo4jGraphBackend
        from src.service.qa_engine.adapters import Neo4jGraphAdapter

        neo4j_uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
        neo4j_user = os.environ.get("NEO4J_USER", "neo4j")
        neo4j_password = os.environ.get("NEO4J_PASSWORD")
        neo4j_database = os.environ.get("NEO4J_DATABASE", "neo4j")

        # 密码必填，没设就直接放弃（避免 driver 抛一个让人困惑的认证错）
        if not neo4j_password:
            _log.warning("[startup] NEO4J_PASSWORD 未设 → 跳过 Neo4j → 不构造真实 retriever")
            return None, None

        neo4j_backend = Neo4jGraphBackend(
            uri=neo4j_uri, user=neo4j_user, password=neo4j_password, database=neo4j_database
        )
        # 试探一下连接（调一个轻量查询）
        _ = neo4j_backend.node_count()
        graph_adapter = Neo4jGraphAdapter(neo4j_backend)
        _log.info("[startup] Neo4j 连接成功: %s", neo4j_uri)
    except Exception as e:
        _log.warning("[startup] Neo4j 连接失败: %s", e)
        return None, None

    # ─── 3) 组装 QARetriever ───
    from src.service.qa_engine.retriever import QARetriever
    retriever = QARetriever(business_store=biz_adapter, graph=graph_adapter)

    # ─── 4) v1.2：装好 MCP-style 工具集（不强制 chat 路径用，先挂上） ───
    # 顺手存到 graph_adapter 上不太合适；改成在外面 app.state 上挂
    # 这里只构造 + 返回；调用方负责挂到 app.state
    from src.service.qa_engine.tools import build_default_registry
    tool_registry = build_default_registry(graph=graph_adapter, business_store=biz_adapter)
    # 暂存到 retriever 实例上当 hint；外层会把它真正放到 app.state
    retriever._tool_registry_hint = tool_registry  # type: ignore[attr-defined]

    return retriever, graph_adapter


@app.on_event("shutdown")
async def close_qa_engine() -> None:
    """关闭时释放资源：主要是 Neo4j driver。"""
    # getattr 第二参是默认值，没注入也安全
    adapter = getattr(app.state, "qa_neo4j_adapter", None)
    if adapter is not None:
        adapter.close()


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
