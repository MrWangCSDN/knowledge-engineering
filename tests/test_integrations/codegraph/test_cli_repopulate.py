"""CLI 装配单测：从 project dict 解析出 repo_root / project_id / weaviate 配置。"""
from src.integrations.codegraph.cli_repopulate import resolve_repopulate_args


def test_resolve_args_from_config():
    cfg = {
        "repo": {"path": "/repos/mall-swarm", "project_id": "mall-swarm"},
        "knowledge": {"vectordb-code": {
            "weaviate_url": "http://localhost:8080", "weaviate_grpc_port": 50051,
            "weaviate_api_key": "k", "collection_name": "CodeEntity", "dimension": 1024,
        }},
    }
    args = resolve_repopulate_args(cfg)
    assert args["repo_root"] == "/repos/mall-swarm"
    assert args["project_id"] == "mall-swarm"
    assert args["weaviate"]["collection_name"] == "CodeEntity"
    assert args["weaviate"]["dimension"] == 1024
