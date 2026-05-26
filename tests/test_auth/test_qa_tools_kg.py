"""验证 5 个 ke_* 知识图谱工具。

测试策略：
  - 用 mock GraphProto / BusinessStoreProto 注入到 build_xxx 工厂函数
  - 验证 handler 输入 dict / 输出 dict / 调用底层方法的方式
  - 不打真实 Weaviate / Neo4j（那是 e2e 测试的事）
"""
from unittest.mock import MagicMock

import pytest

from src.service.qa_engine.tools.base import Tool
from src.service.qa_engine.tools.ke_business_interp import build_ke_business_interp_tool
from src.service.qa_engine.tools.ke_callees import build_ke_callees_tool
from src.service.qa_engine.tools.ke_callers import build_ke_callers_tool
from src.service.qa_engine.tools.ke_search import build_ke_search_tool
from src.service.qa_engine.tools.ke_table_access import build_ke_table_access_tool


# ───────── RED 5: ke_callees 工具 ─────────


@pytest.mark.asyncio
async def test_ke_callees_returns_successors_for_entity() -> None:
    """ke_callees(entity_id=X, max_nodes=5) 应该返回 graph.successors(X) 截断到 5。"""
    # mock 一个图：调用 successors("A") 返回 ['B', 'C', 'D']
    graph = MagicMock()
    graph.successors.return_value = ["B", "C", "D"]

    # build_ke_callees_tool(graph) 返回一个 Tool 实例
    tool = build_ke_callees_tool(graph)

    # 验证元数据
    assert isinstance(tool, Tool)
    assert tool.name == "ke_callees"
    # description 是给 LLM 看的，必须用自然语言；这里只断言非空
    assert tool.description
    # input_schema 应该至少声明 entity_id 是必填
    schema = tool.input_schema
    assert schema.get("type") == "object"
    assert "entity_id" in schema.get("required", [])

    # 调 handler
    out = await tool.handler({"entity_id": "A"})
    # 返回 dict，包含 callees 字段；图原样透传
    assert out == {"entity_id": "A", "callees": ["B", "C", "D"]}
    # mock 被调用时参数验证
    graph.successors.assert_called_once_with("A")


@pytest.mark.asyncio
async def test_ke_callees_respects_max_nodes() -> None:
    """max_nodes 参数应该截断结果列表。"""
    graph = MagicMock()
    graph.successors.return_value = ["B", "C", "D", "E", "F"]
    tool = build_ke_callees_tool(graph)

    out = await tool.handler({"entity_id": "A", "max_nodes": 2})
    assert out["callees"] == ["B", "C"]


@pytest.mark.asyncio
async def test_ke_callees_missing_entity_id_returns_empty_callees() -> None:
    """输入没 entity_id 时优雅返回空列表，不抛错。

    LLM 给的 input 不总是完美；handler 应该宽容，让 LLM 看到错误信号但不崩。
    """
    graph = MagicMock()
    tool = build_ke_callees_tool(graph)

    out = await tool.handler({})
    # 返回 callees=[] + error 字段提示
    assert out["callees"] == []
    assert "error" in out


# ───────── RED 6: ke_callers 工具（对称的上游）─────────


@pytest.mark.asyncio
async def test_ke_callers_returns_predecessors_for_entity() -> None:
    """ke_callers(entity_id=X) 应该返回 graph.predecessors(X)。"""
    graph = MagicMock()
    graph.predecessors.return_value = ["Z", "Y"]

    tool = build_ke_callers_tool(graph)
    assert tool.name == "ke_callers"

    out = await tool.handler({"entity_id": "A"})
    assert out == {"entity_id": "A", "callers": ["Z", "Y"]}
    graph.predecessors.assert_called_once_with("A")


@pytest.mark.asyncio
async def test_ke_callers_handles_graph_error_gracefully() -> None:
    """图谱异常时返回 error 字段，不抛出（不要让 LLM 调一次工具就 5xx）。"""
    graph = MagicMock()
    graph.predecessors.side_effect = RuntimeError("Neo4j 断连")
    tool = build_ke_callers_tool(graph)

    out = await tool.handler({"entity_id": "A"})
    assert out["callers"] == []
    assert "Neo4j" in out["error"]


# ───────── RED 7: ke_search 工具 ─────────


@pytest.mark.asyncio
async def test_ke_search_returns_candidates_from_business_store() -> None:
    """ke_search(query=X, project_id=P) → 拿到 BusinessInterpretation 命中。"""
    store = MagicMock()
    # store.search_method_hits_by_text 是 Protocol 定义的同步方法
    store.search_method_hits_by_text.return_value = [
        {"entity_id": "M1", "summary_text": "x", "level": "api", "score": 0.8},
        {"entity_id": "C1", "summary_text": "y", "level": "class", "score": 0.7},
    ]

    tool = build_ke_search_tool(store, "test")
    assert tool.name == "ke_search"

    out = await tool.handler({
        "query": "兽医列表",
        "project_id": "petclinic",
        "limit": 5,
    })

    # 返回 results 字段（list），含 store 返回的所有 dict
    assert out["query"] == "兽医列表"
    assert len(out["results"]) == 2
    assert out["results"][0]["entity_id"] == "M1"

    # store 被以正确参数调用
    # 注意：project_id 由闭包注入（build_ke_search_tool 第二参数），
    # handler input 里的 "project_id" 字段被 Task1 改动后忽略
    call_kwargs = store.search_method_hits_by_text.call_args.kwargs
    assert call_kwargs["text"] == "兽医列表"
    assert call_kwargs["project_id"] == "test"  # 来自闭包，非 handler input
    assert call_kwargs["limit"] == 5


@pytest.mark.asyncio
async def test_ke_search_default_limit_when_omitted() -> None:
    """没传 limit 时用默认值（5）。"""
    store = MagicMock()
    store.search_method_hits_by_text.return_value = []
    tool = build_ke_search_tool(store, "test")

    await tool.handler({"query": "x", "project_id": "p"})

    call_kwargs = store.search_method_hits_by_text.call_args.kwargs
    assert call_kwargs["limit"] == 5  # 默认值


@pytest.mark.asyncio
async def test_ke_search_missing_query_returns_empty() -> None:
    """query 为空时返回 [] + error，不调底层 store。"""
    store = MagicMock()
    tool = build_ke_search_tool(store, "test")

    out = await tool.handler({"project_id": "p"})  # 缺 query
    assert out["results"] == []
    assert "error" in out
    # 底层 store 不应被调用（保护接口契约）
    store.search_method_hits_by_text.assert_not_called()


# ───────── RED 8: ke_business_interp 工具（按 entity_id 取完整业务解读）─────────


@pytest.mark.asyncio
async def test_ke_business_interp_returns_full_record_for_entity() -> None:
    """ke_business_interp(entity_id=X) → 返回该实体的完整业务解读 dict。

    跟 ke_search 互补：前者是『模糊搜』，这个是『按 ID 精确查』。
    LLM 看到入口方法后想看它的完整业务说明 → 调这个。
    """
    store = MagicMock()
    store.get_by_entity.return_value = {
        "entity_id": "method//X1",
        "entity_type": "method",
        "level": "api",
        "summary_text": "完整业务解读……",
        "business_domain": "医疗诊疗",
        "business_capabilities": "兽医管理",
    }

    tool = build_ke_business_interp_tool(store)
    assert tool.name == "ke_business_interp"

    out = await tool.handler({"entity_id": "method//X1"})
    # 整条 record 透传
    assert out["interpretation"]["entity_id"] == "method//X1"
    assert out["interpretation"]["business_capabilities"] == "兽医管理"
    # 底层调用参数验证
    store.get_by_entity.assert_called_once_with("method//X1", level=None)


@pytest.mark.asyncio
async def test_ke_business_interp_with_level_filter() -> None:
    """level 参数透传到 store.get_by_entity，让结果限制在 class / api / module 等。"""
    store = MagicMock()
    store.get_by_entity.return_value = {"entity_id": "class//C1", "level": "class"}

    tool = build_ke_business_interp_tool(store)
    out = await tool.handler({"entity_id": "class//C1", "level": "class"})

    assert out["interpretation"]["level"] == "class"
    store.get_by_entity.assert_called_once_with("class//C1", level="class")


@pytest.mark.asyncio
async def test_ke_business_interp_returns_null_when_not_found() -> None:
    """实体不存在时 store 返回 None；tool 返回 interpretation=None + error 字段。"""
    store = MagicMock()
    store.get_by_entity.return_value = None
    tool = build_ke_business_interp_tool(store)

    out = await tool.handler({"entity_id": "method//missing"})
    assert out["interpretation"] is None
    assert "not found" in out["error"].lower()


# ───────── RED 9: ke_table_access 工具 ─────────


@pytest.mark.asyncio
async def test_ke_table_access_combines_graph_and_summary_sources() -> None:
    """ke_table_access(entity_id, summary_text?) 应该合并两个来源：
       1) 图谱的 accesses_table 边
       2) summary_text 里的『<table> 表』模式（兜底，没图边时仍有信号）

    去重保序：图边优先；summary 抽出的表如果已经在图边里就不重复。
    """
    graph = MagicMock()
    # graph 的 successors(entity_id, rel_type='accesses_table') 返回有 'vets' 边
    def succ(nid: str, rel_type=None) -> list[str]:
        if rel_type == "accesses_table" and nid == "M1":
            return ["vets"]
        return []
    graph.successors.side_effect = succ

    tool = build_ke_table_access_tool(graph)
    assert tool.name == "ke_table_access"

    # 输入：entity_id + 可选 summary_text（用来抽『visits 表』『vets 表』）
    out = await tool.handler({
        "entity_id": "M1",
        "summary_text": "查询 vets 表 + 写入 visits 表",
    })

    # 应该有 2 张表：vets（图边 + summary 双中）和 visits（仅 summary）
    table_ids = [t["table_id"] for t in out["tables"]]
    assert "vets" in table_ids
    assert "visits" in table_ids
    # vets 在前（图边优先）
    assert table_ids.index("vets") < table_ids.index("visits")
    # 操作类型不同：图边的是 unknown（来自既有的 _extract_table_access），summary 的是 mentioned
    by_id = {t["table_id"]: t for t in out["tables"]}
    assert by_id["vets"]["operation"] in {"unknown", "mentioned"}
    assert by_id["visits"]["operation"] == "mentioned"


@pytest.mark.asyncio
async def test_ke_table_access_no_summary_no_graph_edges_returns_empty() -> None:
    """没图边、没 summary_text → 返回 tables=[]，不抛错。"""
    graph = MagicMock()
    graph.successors.return_value = []
    tool = build_ke_table_access_tool(graph)
    out = await tool.handler({"entity_id": "M1"})
    assert out["tables"] == []


@pytest.mark.asyncio
async def test_ke_table_access_missing_entity_id() -> None:
    graph = MagicMock()
    tool = build_ke_table_access_tool(graph)
    out = await tool.handler({})
    assert out["tables"] == []
    assert "error" in out
