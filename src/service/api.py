"""服务层 REST API：检索、影响分析、图谱子图。

路由优先通过 ``Depends(get_app_context)`` 注入 `AppContext`，便于测试覆盖与逐步摆脱隐式单例。
``set_global_graph`` / ``set_global_config`` 仍保留，委托给 ``AppContext.get()``。
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware

from src.core.context import AppContext, get_app_context
from src.knowledge import KnowledgeGraph
from src.service.auth_dependencies import get_current_user
from src.service.auth_models import User
from src.service.deps_infra import require_infra_healthy  # 抽到独立文件避免 router 循环 import
from src.service.admin_router import router as admin_router
from src.service.auth_router import router as auth_router
from src.service.archived_router import router as archived_router  # Task 6：跨工程归档 session 汇总
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
    """供前端等消费方使用：返回图实例或 None（未构建时）。"""
    return get_app_context().get_graph_optional()


app = FastAPI(
    title="代码知识工程 API",
    description="检索、影响分析、图谱可视化数据",
    version="0.1.0",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(auth_router)
app.include_router(archived_router)  # Task 6：跨工程归档列表
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

    v2.0 Task 22 / Task 3：
      - 只保存底层 singleton 资源：weaviate_interp_store、neo4j_backend、llm
      - 不再预构造 qa_retriever / qa_tools（改为 per-request 构造，见 qa_router.py）
      - synthesizer / router 仍是 singleton（无状态，线程安全）

    向后兼容：
      - 测试 fixture 可预先把 qa_retriever 挂到 app.state；qa_router.py 的
        explain endpoint 会检测到 qa_retriever 存在时走"旧路径"，不走 per-request 构造
        （纯粹为了不破现有测试）

    环境变量（写在 .env.local）：
      WEAVIATE_URL         默认 http://localhost:8080
      WEAVIATE_GRPC_PORT   默认 50051
      WEAVIATE_API_KEY     （可选，私有 Weaviate 需要）
      NEO4J_URI            默认 bolt://localhost:7687
      NEO4J_USER           默认 neo4j
      NEO4J_PASSWORD       必填（生产用）；缺失时只走 StubRetriever
    """
    # hasattr 判断属性是否存在；测试场景里测试夹具可能预先注入了 qa_retriever
    # （向后兼容：测试注入了就跳过真实初始化）
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
    # Python 默认 root logger level = WARNING，导致 INFO 被吞；
    # 显式设置本 logger 为 INFO，让 startup 日志在任何 uvicorn 配置下都可见
    if not _log.handlers:
        # 只在没有 handler 时添加（避免重复输出）；Handler 继承自 StreamHandler 输出到 stderr
        _h = logging.StreamHandler()
        _h.setFormatter(logging.Formatter("%(levelname)s:     %(name)s: %(message)s"))
        _log.addHandler(_h)
    _log.setLevel(logging.INFO)

    # ──── 基础设施健康检查 — 写 app.state.infra_status ────────────────────
    # 设计：[[基础设施健康检查与产品不可用-设计]] §3.2.1
    # 5 个 critical 依赖并发 ping，每个 5s timeout；任一挂 uvicorn 仍 ready，
    # 但 require_infra_healthy dependency 会拒所有需要 critical 资源的路由。
    # 注意：check_all_deps 只从 os.environ 读 config，不依赖 app.state 中的连接对象，
    # 因此必须在 LLM / 后端资源初始化之前运行，保证任一早返回路径也能写入 infra_status。
    from src.service.infra_health import check_all_deps
    # check_all_deps 是协程（async def），必须 await；返回 InfraStatus dict
    app.state.infra_status = await check_all_deps(app.state)
    # 简洁 log 一行显示哪些依赖 ok / 哪些挂
    _log.info(
        "[startup] infra_status: %s",
        {k: v["ok"] for k, v in app.state.infra_status.items()},
    )
    # 任一不 ok 则 WARNING 级别 log 详细错误，方便运维定位
    unhealthy_deps = {k: v for k, v in app.state.infra_status.items() if not v["ok"]}
    if unhealthy_deps:
        _log.warning(
            "[startup] critical 依赖部分不可用（系统将进入「产品不可用」状态）：%s",
            unhealthy_deps,
        )

    # 先初始化 LLM —— 这是 chat 的"必备"组件，挂了就别拉后端资源了
    try:
        llm = DashScopeProvider()
    except Exception as e:
        # `from None` 是 Python 异常链的"切断"语法；这里直接打日志不抛出
        _log.error("[startup] DashScope LLM 初始化失败: %s", e)
        # LLM 挂了，所有下游都没意义；清零 state
        app.state.weaviate_interp_store = None
        app.state.neo4j_backend = None
        app.state.llm = None
        app.state.qa_synthesizer = None
        app.state.qa_router = None
        return

    # ── 试图连接底层存储（获取 singleton 资源） ─────────────────────────────
    neo4j_backend, code_store, interp_store = _try_connect_backends()

    if neo4j_backend is not None and interp_store is not None:
        # Task 22：只存底层 singleton 资源；qa_retriever / qa_tools 改为 per-request 构造
        app.state.weaviate_interp_store = interp_store
        app.state.neo4j_backend = neo4j_backend
        app.state.weaviate_code_store = code_store
        app.state.llm = llm
        _log.info("[startup] 底层资源就绪：Weaviate + Neo4j（per-request retriever 模式）")
    else:
        # 后端不可用 → 挂占位 StubRetriever（向后兼容：qa_router.py 检测 qa_retriever 走旧路径）
        from src.service.qa_engine.stub_retriever import StubRetriever
        app.state.weaviate_interp_store = None
        app.state.neo4j_backend = None
        app.state.weaviate_code_store = None
        app.state.llm = llm
        # 兼容旧路径：qa_router.py explain 检查 qa_retriever；
        # 后端未就绪时仍提供 StubRetriever 让 chat 能跑（context 为空）
        app.state.qa_retriever = StubRetriever()
        app.state.qa_tools = None
        _log.warning("[startup] 后端未就绪 → 使用 StubRetriever（context 将为空）")

    # v1.3：根据环境变量 KE_QA_USE_REACT 决定用 QASynthesizer 还是 ReActSynthesizer
    # 默认关闭（保留 v1.2 的稳定路径）；设 KE_QA_USE_REACT=1 才启用 ReAct 循环
    use_react = os.environ.get("KE_QA_USE_REACT", "").strip() in {"1", "true", "yes"}
    if use_react:
        # ReActSynthesizer 在 per-request 路径里由 build_tools_for_project 提供 registry；
        # 这里构造时不绑定 tool_registry（留给 per-request 注入）
        # 如果 neo4j_backend 未就绪，app.state.qa_tools 为 None，ReAct 也会降级
        from src.service.qa_engine.react_synthesizer import ReActSynthesizer
        max_iter = int(os.environ.get("KE_QA_REACT_MAX_ITER", "12"))
        # per-request 模式下：tool_registry 由 explain endpoint 注入；
        # 这里先不绑（react_synthesizer 接受 None tool_registry 会降级到 single-shot）
        # TODO(Task 24): ReActSynthesizer 改为接受 per-request tool_registry 参数
        tools_hint = getattr(app.state, "qa_tools", None)
        app.state.qa_synthesizer = ReActSynthesizer(
            llm_provider=llm,
            tool_registry=tools_hint,
            max_iterations=max_iter,
        )
        _log.info(
            "[startup] qa_engine ready (model=%s, mode=ReAct, max_iter=%d)",
            llm.model, max_iter,
        )
    else:
        app.state.qa_synthesizer = QASynthesizer(llm_provider=llm)
        _log.info("[startup] qa_engine ready (model=%s, mode=QA-single-shot)", llm.model)

    # SkillRouter：纯关键词路径无依赖；LLM provider 注入后 route_async 才有 fallback 能力
    # 当前 SSE 链路只调同步 router.route()，所以连 None 也能跑，
    # 但既然 LLM 已经在手就一并注入，未来 route_async 想用立刻可用。
    app.state.qa_router = SkillRouter(llm_provider=llm)


def _try_connect_backends() -> tuple[Any, Any, Any]:
    """连接底层存储资源；任意后端失败就返回 (None, None, None)。

    Task 22/Task 3：只返回 singleton 原始 store 对象（Neo4jGraphBackend +
    WeaviateVectorStore + WeaviateTopologicalInterpretStore）。
    不再预构造 adapter / retriever / tool_registry；
    这些改由 qa_router.py 的 build_retriever_for_project / build_tools_for_project
    在每个请求时按 project_id 按需构造。

    分两步：
      1. 连 Neo4j
      2. 连 Weaviate（CodeEntity + TopologicalInterpretation）

    Neo4j 失败视作整体失败 → 退回 StubRetriever（避免半连接状态）。
    Weaviate store 失败不致命：对应工具优雅不注册。

    :return: (Neo4jGraphBackend 或 None,
              WeaviateVectorStore 或 None,
              WeaviateTopologicalInterpretStore 或 None)
    """
    import os
    import logging

    _log = logging.getLogger("qa_engine.startup")

    weaviate_url = os.environ.get("WEAVIATE_URL", "http://localhost:8080")
    weaviate_grpc_port = int(os.environ.get("WEAVIATE_GRPC_PORT", "50051"))
    weaviate_api_key = os.environ.get("WEAVIATE_API_KEY") or None
    # 维度固定 1024（bge-m3 输出维度）；如果将来换 embedding 模型再环境变量化
    weaviate_dimension = int(os.environ.get("WEAVIATE_DIMENSION", "1024"))

    # ─── 1) Neo4j ───
    try:
        from src.knowledge.graph_neo4j import Neo4jGraphBackend

        neo4j_uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
        neo4j_user = os.environ.get("NEO4J_USER", "neo4j")
        neo4j_password = os.environ.get("NEO4J_PASSWORD")
        neo4j_database = os.environ.get("NEO4J_DATABASE", "neo4j")

        # 密码必填，没设就直接放弃（避免 driver 抛一个让人困惑的认证错）
        if not neo4j_password:
            _log.warning("[startup] NEO4J_PASSWORD 未设 → 跳过 Neo4j → 不构造真实 retriever")
            return None, None, None

        neo4j_backend = Neo4jGraphBackend(
            uri=neo4j_uri, user=neo4j_user, password=neo4j_password, database=neo4j_database
        )
        # 试探一下连接（调一个轻量查询）
        _ = neo4j_backend.node_count()
        _log.info("[startup] Neo4j 连接成功: %s", neo4j_uri)
    except Exception as e:
        _log.warning("[startup] Neo4j 连接失败: %s", e)
        return None, None, None

    # ─── 2) Weaviate CodeEntity + TopologicalInterpretation store ───
    # 连接失败不致命：返回 None，对应工具优雅不注册（build_default_registry 条件注册）
    code_store = None
    interp_store = None
    try:
        from src.knowledge.vector_store_weaviate import WeaviateVectorStore
        code_store = WeaviateVectorStore(
            url=weaviate_url,
            grpc_port=weaviate_grpc_port,
            dimension=weaviate_dimension,
            api_key=weaviate_api_key,
        )
        _log.info("[startup] Weaviate CodeEntity store 连接成功")
    except Exception as e:
        _log.warning("[startup] Weaviate CodeEntity store 连接失败（ke_read_entity 不可用）: %s", e)
    try:
        from src.knowledge.weaviate_interpretation_store import WeaviateTopologicalInterpretStore
        interp_store = WeaviateTopologicalInterpretStore(
            url=weaviate_url,
            grpc_port=weaviate_grpc_port,
            dimension=weaviate_dimension,
            api_key=weaviate_api_key,
        )
        _log.info("[startup] Weaviate TopologicalInterpretation store 连接成功: %s", weaviate_url)
    except Exception as e:
        _log.warning("[startup] Weaviate TopologicalInterpretation store 连接失败（ke_search 不可用）: %s", e)

    # Task 22/Task 3：只返回原始 singleton 资源，不在这里预构造 adapter / retriever / tools
    return neo4j_backend, code_store, interp_store


@app.on_event("shutdown")
async def close_qa_engine() -> None:
    """关闭时释放资源：主要是 Neo4j driver。

    Task 22：shutdown 改为关闭 app.state.neo4j_backend（singleton 资源），
    不再关闭 qa_neo4j_adapter（已删除）。
    向后兼容：如果旧版 adapter 还在 app.state，也一并尝试关闭。
    """
    import logging
    _log = logging.getLogger("qa_engine.startup")

    # Task 22 新路径：关闭 singleton neo4j_backend
    backend = getattr(app.state, "neo4j_backend", None)
    if backend is not None:
        try:
            backend.close()
            _log.info("[shutdown] neo4j_backend 已关闭")
        except Exception as e:
            _log.warning("[shutdown] neo4j_backend 关闭时出错: %s", e)

    # 向后兼容旧路径（如果旧版 adapter 还挂着）
    adapter = getattr(app.state, "qa_neo4j_adapter", None)
    if adapter is not None:
        try:
            adapter.close()
        except Exception:
            pass


@app.get("/health")
async def health(request: Request) -> dict:
    """主动重新 ping 5 个 critical 依赖，更新 app.state.infra_status 并返回。

    设计：[[基础设施健康检查与产品不可用-设计]] §3.2.3

    Response schema:
      - 未登录 / 普通用户：{"healthy": bool, "ts": iso}
      - Admin            ：{"healthy": bool, "ts": iso, "deps": {...}}

    本端点本身不附 require_infra_healthy（决策 #5：健康检查不能自我熔断）。
    用户权限通过 Authorization header 解析；缺 header → 视作匿名（不暴露 deps）。
    """
    # 导入时间工具；datetime.now(timezone.utc) 返回带时区的当前 UTC 时间
    from datetime import datetime, timezone

    # 每次调用都重新 ping，不读 cache（用户「重试连接」必须看到最新状态）
    # 局部导入避免循环依赖，同时让 patch("src.service.infra_health.check_all_deps") 可以正确 mock
    from src.service.infra_health import check_all_deps

    # await 关键字等待协程完成；check_all_deps 是 async def，必须 await
    status = await check_all_deps(request.app.state)
    # 写回 state，让 require_infra_healthy 下一次也能用上最新值
    request.app.state.infra_status = status

    # all() 接受可迭代对象，全 True 才返回 True；字典的 .values() 返回所有 value 的视图
    healthy = all(v.get("ok") for v in status.values())
    # 类型注解 dict：说明变量类型，帮助 IDE 做静态检查
    body: dict = {
        "healthy": healthy,
        # .isoformat() 将 datetime 转为 ISO 8601 字符串，如 "2026-05-27T10:00:00+00:00"
        "ts": datetime.now(timezone.utc).isoformat(),
    }

    # 尝试解析当前用户（不强制登录）— admin 看 deps，其他不看
    # request.headers 是一个类字典对象，.get() 返回指定 header 值，缺失时返回默认值 ""
    auth_header = request.headers.get("Authorization", "")
    # str.startswith() 检查字符串是否以指定前缀开头
    if auth_header.startswith("Bearer "):
        # 切片语法 [n:] 取从第 n 个字符开始的子字符串；len("Bearer ") == 7
        token = auth_header[len("Bearer "):]
        try:
            # decode_token 解码 JWT；签名错或过期返回 None（见 auth_security.py）
            from src.service.auth_security import decode_token
            from src.service.db import get_session_maker
            from src.service.auth_models import User

            # payload 是 dict 或 None；若 token 无效则为 None
            payload = decode_token(token)
            if payload is not None:
                # payload.get("sub", "0") 取 subject 字段（用户 ID 字符串），缺失时返回 "0"
                user_id = int(payload.get("sub", "0"))
                if user_id > 0:
                    # get_session_maker() 返回 async_sessionmaker 工厂函数，调用它得到 session
                    SM = get_session_maker()
                    # async with ... as s：异步上下文管理器，自动关闭 session
                    async with SM() as s:
                        # s.get(Model, pk) 按主键查找；找不到返回 None
                        user = await s.get(User, user_id)
                        # 只有真实存在且 is_admin=True 的用户才暴露 deps
                        if user is not None and user.is_admin:
                            body["deps"] = status
        except Exception:
            # token 错 / decode 失败 / DB 异常 → 静默忽略，按匿名处理
            # pass 是 Python 的空语句，什么都不做
            pass

    return body


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

    # 启动 cwd 与仓库根可能不一致；除 cwd 外再尝试本包所在项目根下的默认配置
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
    供消费方使用：若已配置 Neo4j 则返回 Neo4jGraphBackend 实例，否则返回 None。
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
