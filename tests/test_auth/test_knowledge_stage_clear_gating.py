"""KnowledgeStage Weaviate clear 门控单元测试（多工程隔离 Phase1-T2）。

Mock-based，不连真 Weaviate / Neo4j / MySQL。验证 KnowledgeStage.execute 的
Weaviate 清理段：
  - force_full=False（续跑，默认）：vs.clear **完全不被调用**（保留向量 + checkpoint）。
  - force_full=True：vs.clear 以 tenant=project_id 被调用恰好一次（只清当前工程租户）。

实现手段：patch 掉 VectorStoreFactory.create 返回一个 MagicMock vs（捕获 clear 调用），
patch 掉 KnowledgeGraph 避免真建图 / 真写后端；graph_backend 设非 neo4j 跳过图清理段。
"""
# unittest.mock：标准库 mock 工具，MagicMock 造假对象、patch 临时替换名字
from unittest.mock import MagicMock, patch

# 被测对象：KnowledgeStage 及其上下文
from src.pipeline.stage_runtime import KnowledgeStage, KnowledgeStageContext


def _make_ctx(*, force_full: bool, project_id: str = "p1") -> KnowledgeStageContext:
    """构造一个最小可执行的 KnowledgeStageContext。

    knowledge_cfg 用 MagicMock 模拟：
      - graph.backend = "memory"   → 跳过 Neo4j 清理段（本测试只关注 Weaviate）
      - vectordb_code.enabled=True / backend="weaviate" → 进入 Weaviate 清理段
    """
    # 假 knowledge_cfg：MagicMock 默认所有属性可访问
    cfg = MagicMock()
    # graph_backend != "neo4j" → if graph_backend == "neo4j" 分支整体跳过
    cfg.graph.backend = "memory"
    # 进入 Weaviate 清理段的两个前置条件
    cfg.vectordb_code.enabled = True
    cfg.vectordb_code.backend = "weaviate"
    cfg.vectordb_code.dimension = 1024
    cfg.vectordb_code.allow_fallback_to_memory = False
    cfg.vectordb_code.weaviate_url = "http://fake:8080"
    cfg.vectordb_code.weaviate_grpc_port = 50051
    cfg.vectordb_code.collection_name = "CodeEntity"
    cfg.vectordb_code.weaviate_api_key = None

    # KnowledgeStageContext 是 dataclass，继承链上还有 step_callback / knowledge_cfg 等字段
    return KnowledgeStageContext(
        step_callback=lambda _msg: None,   # 空回调，吞掉步骤文案
        knowledge_cfg=cfg,
        structure_facts=MagicMock(),
        semantic_facts=MagicMock(),
        domain=MagicMock(),
        run_interpret_phase=False,
        interpret_enabled=False,
        progress_callback=None,
        project_id=project_id,
        force_full=force_full,
    )


def _run_stage_capturing_vs(ctx) -> MagicMock:
    """执行 KnowledgeStage.execute，返回 VectorStoreFactory.create 产出的假 vs。

    patch 两处避免真后端：
      - VectorStoreFactory.create → 返回一个 MagicMock（捕获 clear / close 调用）
      - KnowledgeGraph → MagicMock 类，build_from 不真跑
    patch 目标是 stage_runtime 模块里导入后的名字（from ... import KnowledgeGraph /
    VectorStoreFactory），所以路径是 src.pipeline.stage_runtime.XXX。
    """
    fake_vs = MagicMock()
    # AppContext.set_graph 在 execute 末尾被调用；patch 掉 AppContext.get 返回假 ctx
    with patch("src.pipeline.stage_runtime.VectorStoreFactory") as factory, \
         patch("src.pipeline.stage_runtime.KnowledgeGraph") as graph_cls, \
         patch("src.pipeline.stage_runtime.AppContext") as app_ctx:
        factory.create.return_value = fake_vs
        graph_cls.return_value = MagicMock()
        app_ctx.get.return_value = MagicMock()
        KnowledgeStage().execute(ctx)
    return fake_vs


def test_clear_not_called_when_not_force_full():
    """force_full=False（续跑）：vs.clear 完全不被调用，保留向量 + checkpoint。"""
    ctx = _make_ctx(force_full=False, project_id="p1")
    fake_vs = _run_stage_capturing_vs(ctx)

    # 续跑不清向量：clear 一次都不能调
    fake_vs.clear.assert_not_called()


def test_clear_called_with_tenant_when_force_full():
    """force_full=True：vs.clear 以 tenant=project_id 被调用恰好一次（只清本工程租户）。"""
    ctx = _make_ctx(force_full=True, project_id="mall-swarm")
    fake_vs = _run_stage_capturing_vs(ctx)

    # 全量重建：只清当前工程的 tenant，不删整集合
    fake_vs.clear.assert_called_once_with(tenant="mall-swarm")
