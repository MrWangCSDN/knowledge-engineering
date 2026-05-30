# src/integrations/codegraph/cli_repopulate.py
"""CLI：从 project.yaml 跑 CodeEntity 重灌。

用法：python -m src.integrations.codegraph.cli_repopulate --config config/project.yaml [--force]
真依赖（DashScope embed / Weaviate store）在 main() 里装配；resolve_repopulate_args 纯解析便于单测。
"""
from __future__ import annotations  # PEP 563：允许类型注解里提前引用未定义的类名


def resolve_repopulate_args(cfg: dict) -> dict:
    """从 project.yaml dict 解析重灌需要的参数（纯函数，便于单测）。

    Args:
        cfg: project.yaml 反序列化后的字典，顶层含 repo 段和 knowledge 段
    Returns:
        含 repo_root / project_id / weaviate 子 dict 的参数字典
    """
    repo = cfg.get("repo") or {}                    # 读 repo 段；若缺失则给空 dict 避免 NoneType 报错
    # 取 knowledge.vectordb-code 段（代码向量库专用配置）；多层 or {} 防 None
    vc = ((cfg.get("knowledge") or {}).get("vectordb-code")) or {}
    return {
        "repo_root": repo.get("path"),              # 工程源码根（读片段 + 定位 .codegraph.db）
        # project_id 优先取 repo.project_id；缺失时降级用 path 作为租户 ID（兼容老配置）
        "project_id": repo.get("project_id") or repo.get("path"),
        "weaviate": {
            "weaviate_url": vc.get("weaviate_url"),
            "weaviate_grpc_port": vc.get("weaviate_grpc_port"),
            "weaviate_api_key": vc.get("weaviate_api_key"),
            # collection_name 默认 "CodeEntity"（与 Weaviate Schema 一致）
            "collection_name": vc.get("collection_name") or "CodeEntity",
            # dimension 默认 1024（DashScope text-embedding-v3 输出维度）
            "dimension": vc.get("dimension") or 1024,
        },
    }


def main() -> None:  # pragma: no cover  （真跑入口，需全栈，不单测）
    """装配真依赖并跑全量重灌。"""
    import argparse  # 标准库：命令行参数解析，argparse 会自动生成 --help 文档
    import yaml      # 第三方库：解析 YAML 配置文件；pip install pyyaml

    # 延迟导入真实依赖（避免单测时触发 Weaviate/DashScope 连接）
    from src.semantic.embedding import get_embeddings_batch          # 批量向量化
    from src.semantic.embedding_checkpoint import EmbeddingCheckpoint  # 断点续跑 checkpoint
    from src.knowledge.vector_store_weaviate import WeaviateVectorStore  # 向量库
    from src.integrations.codegraph.db import CodeGraphDB            # 只读 SQLite
    from src.integrations.codegraph.paths import codegraph_db_path   # DB 路径推导
    from src.integrations.codegraph.repopulate import repopulate_code_entities  # 重灌主逻辑

    # 定义 CLI 参数；add_argument 注册参数，parse_args 解析 sys.argv
    p = argparse.ArgumentParser(description="重灌 CodeEntity 向量库")
    p.add_argument("--config", default="config/project.yaml", help="project.yaml 路径")
    # store_true：遇到 --force 就把 a.force 置 True，无需额外值
    p.add_argument("--force", action="store_true", help="忽略 checkpoint 全量重 embed")
    a = p.parse_args()

    # open(...) 返回文件对象，yaml.safe_load 把 YAML 文本解析成 Python dict
    cfg = yaml.safe_load(open(a.config, encoding="utf-8"))
    args = resolve_repopulate_args(cfg)
    w = args["weaviate"]  # 取 weaviate 子配置

    # 装配 WeaviateVectorStore（连接 Weaviate 并确保 Schema 存在）
    store = WeaviateVectorStore(
        url=w["weaviate_url"],
        grpc_port=w["weaviate_grpc_port"],
        collection_name=w["collection_name"],
        dimension=w["dimension"],
        api_key=w["weaviate_api_key"],
    )
    # 打开 .codegraph.db 只读 SQLite
    db = CodeGraphDB(codegraph_db_path(args["repo_root"]))
    # 加载 checkpoint：._done 是已完成 durable_key 的集合
    ckpt = EmbeddingCheckpoint.load(args["project_id"], weaviate_store=store)
    # --force 时清空跳过集合（全量重跑）；否则用已完成集合跳过已处理节点
    skip = set() if a.force else ckpt._done

    # 执行全量重灌，传入注入依赖
    stats = repopulate_code_entities(
        db=db,
        repo_root=args["repo_root"],
        project_id=args["project_id"],
        embed_batch=get_embeddings_batch,
        store=store,
        skip_keys=skip,
    )
    print("重灌完成：", stats)  # 打印统计结果到标准输出


if __name__ == "__main__":  # pragma: no cover
    # 直接 python cli_repopulate.py 时运行；python -m ... 时也会走这里
    main()
