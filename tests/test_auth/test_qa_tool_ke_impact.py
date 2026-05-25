# tests/test_auth/test_qa_tool_ke_impact.py
"""
ke_impact：多跳影响闭包（BFS over GraphProto.successors/predecessors）。
direction down=下游闭包（影响谁），up=上游闭包（被谁影响）。
"""
import pytest

from src.service.qa_engine.tools.ke_impact import build_ke_impact_tool


class _FakeGraph:
    """简单有向图：A->B->C，A->D。successors/predecessors 同 GraphProto。"""
    _edges = {"A": ["B", "D"], "B": ["C"], "C": [], "D": []}

    def successors(self, entity_id, rel_type=None):
        return list(self._edges.get(entity_id, []))

    def predecessors(self, entity_id, rel_type=None):
        # 反向边
        return [src for src, dsts in self._edges.items() if entity_id in dsts]


@pytest.mark.asyncio
async def test_impact_down_closure():
    tool = build_ke_impact_tool(_FakeGraph())
    out = await tool.handler({"entity_id": "A", "direction": "down"})
    # A 的下游闭包：B, C, D（不含起点 A 自身）
    assert set(out["nodes"]) == {"B", "C", "D"}
    assert out["count"] == 3
    assert out["direction"] == "down"
    assert out["entity_id"] == "A"


@pytest.mark.asyncio
async def test_impact_up_closure():
    tool = build_ke_impact_tool(_FakeGraph())
    out = await tool.handler({"entity_id": "C", "direction": "up"})
    # 谁能到达 C：B（B->C）、A（A->B->C）
    assert set(out["nodes"]) == {"A", "B"}
    assert out["count"] == 2


@pytest.mark.asyncio
async def test_impact_max_depth_limits_bfs():
    tool = build_ke_impact_tool(_FakeGraph())
    # depth=1：A 只到直接下游 B, D（不含 2 跳的 C）
    out = await tool.handler({"entity_id": "A", "direction": "down", "max_depth": 1})
    assert set(out["nodes"]) == {"B", "D"}


@pytest.mark.asyncio
async def test_impact_missing_entity_id_returns_error():
    tool = build_ke_impact_tool(_FakeGraph())
    out = await tool.handler({"direction": "down"})
    assert out["nodes"] == []
    assert "error" in out


@pytest.mark.asyncio
async def test_impact_invalid_direction_defaults_down():
    tool = build_ke_impact_tool(_FakeGraph())
    # 非法 direction → 兜底当 down
    out = await tool.handler({"entity_id": "A", "direction": "sideways"})
    assert set(out["nodes"]) == {"B", "C", "D"}
    assert out["direction"] == "down"


def test_impact_tool_metadata():
    tool = build_ke_impact_tool(_FakeGraph())
    assert tool.name == "ke_impact"
    assert "entity_id" in tool.input_schema["properties"]
    assert tool.input_schema["required"] == ["entity_id"]
