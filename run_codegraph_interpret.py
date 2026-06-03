"""2b 解读重生 CLI：以 CodeGraph 为源生成业务解读（method_entity_id=qualified_name）。

设计 [[2b解读重生-设计]] §4.3；计划 docs/superpowers/plans/2026-06-03-2b-codegraph-interpretation.md Task 2。
与 run_topological_interpret.py（StructureFacts 源，canonical_v1 id）物理隔离，防误用旧源。

用法（服务器侧，需授权执行）：
  ./venv/bin/python run_codegraph_interpret.py \
      --project-id mall-swarm --repo-path /opt/mall-swarm \
      --codegraph-db /opt/mall-swarm/.codegraph/<x>.codegraph.db \
      --modules mall-admin,mall-portal,mall-common,mall-search,mall-gateway \
      --workers 8 --config config/project.yaml
"""
from __future__ import annotations

# 标准库：命令行解析 + 读环境变量（Weaviate 连接与 ke-api 同源）
import argparse
import os

# 新增主体：CodeGraph → StructureFacts
from src.knowledge.codegraph_facts_provider import CodeGraphFactsProvider
# 零改动复用的拓扑解读器
from src.knowledge.topological_interpreter import TopologicalInterpreter
# 解读库存储（multi-tenancy 集合）；下面用子类绑定 tenant
from src.knowledge.weaviate_interpretation_store import WeaviateTopologicalInterpretStore


class _TenantBoundStore(WeaviateTopologicalInterpretStore):
    """绑定固定 tenant 的解读库 store。

    背景：TopologicalInterpreter 是 multi-tenancy 之前的组件，调用 store 时全程不传 tenant
    （add_with_created/list_existing_method_ids/get_by_method_id/_get_collection().data.delete 都走
    legacy 无租户路径）。而 TopologicalInterpretation 集合已 MT-enabled，QA 召回按 with_tenant 读。
    为"不改解读器"又把数据写到正确 tenant：**重写 _get_collection() 返回 with_tenant(tenant) 视图**——
    store 所有内部方法都经由 _get_collection()，一处 override 即全链路 tenant-scoped。
    """

    def __init__(self, *, tenant: str, **kwargs) -> None:
        # 先按基类构造（连接参数 url/grpc_port/dimension/api_key 经 kwargs 透传）
        super().__init__(**kwargs)
        # 记录绑定的 tenant（= project_id）
        self._bound_tenant = tenant

    def _get_collection(self):  # type: ignore[override]
        """返回 tenant-scoped 集合视图：基类拿到原始 collection 再 .with_tenant(绑定租户)。"""
        # super()._get_collection() 拿基类的原始 Collection；.with_tenant 切到 project 分区
        return super()._get_collection().with_tenant(self._bound_tenant)


def _build_llm(config_path: str):
    """从项目配置构建 LLM provider（与 run_topological_interpret.py 同源）。"""
    # 延迟 import：避免 CLI 模块导入期就拉重依赖（也方便测试 mock 本函数）
    from src.pipeline.config_bootstrap import load_config
    from src.knowledge.llm import LLMProviderFactory
    config = load_config(config_path)
    # method_interpretation 配置段携带 LLM backend 选择
    mi = config.knowledge.method_interpretation
    # from_method_interpretation 解析 backend；.provider 是可调用的 LLM 句柄
    return LLMProviderFactory.from_method_interpretation(mi).provider


def _build_store(project_id: str) -> _TenantBoundStore:
    """构建绑定 project tenant 的解读库 store。

    Weaviate 连接走 **os.environ**（与 ke-api 的 _try_connect_backends 同源），确保批量写到
    QA 实际读取的同一 Weaviate 实例；**不取 config/project.yaml 的 weaviate_url**——实测服务器上
    project.yaml=localhost:8080 而 .env WEAVIATE_URL=真实地址，取 config 会写错实例、QA 读不到。
    集合用基类默认 TopologicalInterpretation。
    """
    return _TenantBoundStore(
        tenant=project_id,
        url=os.environ.get("WEAVIATE_URL", "http://localhost:8080"),
        grpc_port=int(os.environ.get("WEAVIATE_GRPC_PORT", "50051")),
        dimension=int(os.environ.get("WEAVIATE_DIMENSION", "1024")),
        api_key=os.environ.get("WEAVIATE_API_KEY") or None,
    )


def build_and_run(*, db_path: str, repo_path: str, project_id: str,
                  modules: set[str] | None, workers: int,
                  config_path: str = "config/project.yaml") -> dict:
    """装配 FactsProvider → TopologicalInterpreter → run，返回统计。"""
    # 1. CodeGraph → StructureFacts（entity.id = qualified_name）
    provider = CodeGraphFactsProvider(db_path=db_path, repo_local_path=repo_path)
    facts = provider.build_structure_facts(module_filter=modules)
    # 2. LLM（走 config 的 method_interpretation backend）+ tenant-bound store（Weaviate 走 env，写到 project tenant）
    llm = _build_llm(config_path)
    store = _build_store(project_id)
    # 3. 注入零改动复用的拓扑解读器；embedding_dim/language/llm_timeout 用默认（mall-swarm: 1024/zh/90）
    interp = TopologicalInterpreter(
        structure_facts=facts,
        llm=llm,
        weaviate_store=store,
        repo_path=repo_path,
        max_workers=workers,
        # 独立 state 文件，避免与 run_topological_interpret 的状态冲突
        state_file=f"out_ui/2b_interp_state_{project_id}.json",
    )
    return interp.run()


def main() -> None:
    ap = argparse.ArgumentParser(description="2b CodeGraph-backed 解读重生")
    ap.add_argument("--project-id", required=True)
    ap.add_argument("--repo-path", required=True)
    ap.add_argument("--codegraph-db", required=True)
    ap.add_argument("--modules", default="", help="逗号分隔模块白名单；空=全量")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--config", default="config/project.yaml")
    args = ap.parse_args()
    # 解析模块白名单：去空白、过滤空串；全空 → None（全量）
    modules = {m.strip() for m in args.modules.split(",") if m.strip()} or None
    stats = build_and_run(
        db_path=args.codegraph_db, repo_path=args.repo_path,
        project_id=args.project_id, modules=modules, workers=args.workers,
        config_path=args.config,
    )
    print(stats)


if __name__ == "__main__":
    main()
