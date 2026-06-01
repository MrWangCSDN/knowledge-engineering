"""验证 Neo4j 连不上时，_try_connect_backends 仍返回 Weaviate code/interp store（不再 None,None,None）。

背景：CodeGraph 迁移后 QA 图导航不依赖 Neo4j（设计 [[CodeGraph-结构引擎集成-设计]] §7），
但 startup 原来在 Neo4j 失败时会连带把 Weaviate store 也清空 → QA 兜底失效。本测试守护解耦。
"""
# unittest.mock：标准库的打桩工具；patch 临时替换对象，MagicMock 是万能假对象
from unittest.mock import patch, MagicMock

# 被测模块：startup 的后端连接函数所在
import src.service.api as api


def test_neo4j_failure_keeps_weaviate_stores(monkeypatch):
    """Neo4j 构造抛错（模拟不可用）时，code_store / interp_store 仍应被正常构造。"""
    # monkeypatch.setenv：pytest 内置夹具，临时设环境变量，测试结束自动还原
    monkeypatch.setenv("WEAVIATE_URL", "http://x:8080")
    monkeypatch.setenv("WEAVIATE_API_KEY", "k")
    monkeypatch.setenv("NEO4J_PASSWORD", "p")  # 有密码，但下面让连接抛错

    # patch(...) 作为上下文管理器：with 块内替换，块外恢复
    # 1) Neo4jGraphBackend 构造即抛 RuntimeError → 模拟 Neo4j 宕机
    # 2) 两个 Weaviate store 替成 MagicMock（构造成功）→ 验证它们不被 Neo4j 失败连累
    with patch("src.knowledge.graph_neo4j.Neo4jGraphBackend", side_effect=RuntimeError("neo4j down")), \
         patch("src.knowledge.vector_store_weaviate.WeaviateVectorStore", return_value=MagicMock()), \
         patch("src.knowledge.weaviate_interpretation_store.WeaviateTopologicalInterpretStore", return_value=MagicMock()):
        neo4j_backend, code_store, interp_store = api._try_connect_backends()

    # Neo4j 连不上 → None（符合预期）
    assert neo4j_backend is None, "Neo4j 连不上应返回 None"
    # 关键断言：Neo4j 失败不应连累 Weaviate 两个 store
    assert code_store is not None, "Neo4j 失败不应连累 code_store"
    assert interp_store is not None, "Neo4j 失败不应连累 interp_store"
