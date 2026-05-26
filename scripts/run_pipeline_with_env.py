"""Pipeline wrapper：从 .env.local 读真实 Weaviate / Neo4j 连接信息，运行时覆盖 yaml 的 dev placeholder。

为什么需要它：
- yaml 里 weaviate_url / neo4j_uri 是 dev placeholder（localhost），生产部署走 sed 替换。
- service/api.py 已经标准化用 env vars（WEAVIATE_URL / NEO4J_URI / NEO4J_PASSWORD / ...）。
- pipeline cli 只读 yaml，没和 env vars 接轨。本 wrapper 桥接：env > yaml。

不修改原 yaml；生成 /tmp/<project>_pipeline.yaml 临时文件，跑完保留供调试，下次重跑会覆盖。
仅当 env var 存在时才覆盖；缺失则保留 yaml 原值。

用法：
    cd /Users/java/knowledge-engineering-auth
    ./venv/bin/python -m scripts.run_pipeline_with_env --until knowledge --without-interpretation
"""
from __future__ import annotations  # PEP 563 兼容旧 Python

import os  # 用 os.environ 读环境变量
import sys  # 用 sys.argv 透传 CLI 参数到 run_pipeline
import tempfile  # 不直接用，但留个标准库引用提示
from pathlib import Path  # 比 os.path 更易读
import yaml  # PyYAML：解析 yaml 文件
from dotenv import load_dotenv  # python-dotenv：加载 .env.local 到 os.environ


# ─── 工程根 ──────────────────────────────────────────────────────────────────
_root = Path(__file__).resolve().parent.parent  # scripts/.. = 项目根


def _patch_config(yaml_dict: dict) -> dict:
    """把 yaml dict 中的 weaviate / neo4j 三段用 env vars 覆盖（仅当 env 存在时）。

    Returns 修改后的 dict（原地修改 + 返回）。
    """
    # ── Weaviate：三段 vectordb（code / interpret / business） ───────────────
    weaviate_url = os.environ.get("WEAVIATE_URL")
    weaviate_api_key = os.environ.get("WEAVIATE_API_KEY")
    weaviate_grpc_port = os.environ.get("WEAVIATE_GRPC_PORT")
    # knowledge 段下三个 vectordb 字典 key（注意 yaml 里用连字符不是下划线）
    for section in ("vectordb-code", "vectordb-interpret", "vectordb-business"):
        cfg = yaml_dict.get("knowledge", {}).get(section)
        if cfg is None:  # 段不存在跳过
            continue
        if weaviate_url:
            cfg["weaviate_url"] = weaviate_url
        if weaviate_api_key:
            cfg["weaviate_api_key"] = weaviate_api_key
        if weaviate_grpc_port:
            cfg["weaviate_grpc_port"] = int(weaviate_grpc_port)
    # ── Neo4j ──────────────────────────────────────────────────────────────
    graph = yaml_dict.get("knowledge", {}).get("graph")
    if graph is not None:
        neo4j_uri = os.environ.get("NEO4J_URI")
        neo4j_user = os.environ.get("NEO4J_USER")
        neo4j_password = os.environ.get("NEO4J_PASSWORD")
        neo4j_db = os.environ.get("NEO4J_DATABASE")
        if neo4j_uri:
            graph["neo4j_uri"] = neo4j_uri
        if neo4j_user:
            graph["neo4j_user"] = neo4j_user
        if neo4j_password:
            graph["neo4j_password"] = neo4j_password
        if neo4j_db:
            graph["neo4j_database"] = neo4j_db
    return yaml_dict


def main() -> None:
    """加载 yaml + env，生成临时 yaml，调 pipeline.cli 跑。"""
    # 1) 加载 .env.local（不覆盖已设 shell env）
    load_dotenv(_root / ".env.local", override=False)
    load_dotenv(_root / ".env", override=False)

    # 2) 读原 yaml
    src_yaml = _root / "config" / "project.yaml"
    with src_yaml.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # 3) env 覆盖
    cfg = _patch_config(cfg)

    # 4) 决定 tenant / project_id（重要：v2.0 多租户）
    repo_cfg = cfg.get("repo", {})
    project_id = repo_cfg.get("project_id", "default")  # 已在 yaml 里加过

    # 5) 写临时 yaml 到 /tmp/<project_id>_pipeline.yaml
    tmp_yaml = Path("/tmp") / f"{project_id}_pipeline.yaml"
    with tmp_yaml.open("w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    print(f"[wrapper] 临时 yaml 写入: {tmp_yaml} (project_id={project_id})")

    # 6) 透传 sys.argv[1:] 给 pipeline.cli，但把 --config 强制为临时 yaml
    # 移除用户传的 --config / -c（如果有）
    argv_in: list[str] = []  # 过滤后的参数
    skip_next = False
    for i, a in enumerate(sys.argv[1:]):
        if skip_next:
            skip_next = False
            continue
        if a in ("--config", "-c"):
            skip_next = True  # 跳过它的值
            continue
        if a.startswith("--config=") or a.startswith("-c="):
            continue
        argv_in.append(a)

    # 7) 调 pipeline.cli.main()，把临时 yaml 当 --config 注入
    sys.argv = ["pipeline.cli", "--config", str(tmp_yaml), *argv_in]
    print(f"[wrapper] 调用 pipeline.cli with argv: {sys.argv[1:]}")
    from src.pipeline.cli import main as cli_main
    cli_main()


if __name__ == "__main__":
    main()
