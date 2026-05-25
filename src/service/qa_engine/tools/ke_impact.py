"""ke_impact 工具：多跳影响闭包（BFS over GraphProto）。

影响分析杀手级能力——代码知识库能做、通用 LLM 做不到。
direction='down'：沿 successors（它调用谁）做闭包 = 改这个实体会影响哪些下游。
direction='up'  ：沿 predecessors（谁调用它）做闭包 = 哪些上游依赖这个实体。

复用已有 project_id 隔离的 GraphProto.successors/predecessors，在 handler 内 BFS，
不引新后端依赖。
"""
from __future__ import annotations

from collections import deque
from typing import Any

from src.service.qa_engine.retriever import GraphProto
from src.service.qa_engine.tools.base import Tool

# 默认 / 上限：防超大图把闭包跑爆
_DEFAULT_MAX_DEPTH = 5
_MAX_DEPTH_CAP = 20
_DEFAULT_MAX_NODES = 200

_KE_IMPACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entity_id": {
            "type": "string",
            "description": "起始实体 ID，形如 method//xxx 或 class//xxx",
        },
        "direction": {
            "type": "string",
            "description": "down=下游影响闭包（改它影响谁）；up=上游依赖闭包（谁依赖它）",
            "enum": ["down", "up"],
            "default": "down",
        },
        "max_depth": {
            "type": "integer",
            "description": "BFS 最大跳数",
            "default": _DEFAULT_MAX_DEPTH,
            "minimum": 1,
            "maximum": _MAX_DEPTH_CAP,
        },
    },
    "required": ["entity_id"],
}


def build_ke_impact_tool(graph: GraphProto) -> Tool:
    """构造绑定到指定 GraphProto 的 ke_impact Tool。"""

    async def handler(input: dict[str, Any]) -> dict[str, Any]:
        entity_id = input.get("entity_id")
        if not entity_id:
            return {
                "entity_id": None,
                "direction": "down",
                "count": 0,
                "nodes": [],
                "error": "missing required field: entity_id",
            }

        # direction 兜底：非 'up' 一律当 'down'
        direction = input.get("direction")
        if direction != "up":
            direction = "down"

        # max_depth 容错（LLM 可能传 string）+ 夹到 [1, cap]
        try:
            max_depth = int(input.get("max_depth", _DEFAULT_MAX_DEPTH))
        except (TypeError, ValueError):
            max_depth = _DEFAULT_MAX_DEPTH
        max_depth = max(1, min(max_depth, _MAX_DEPTH_CAP))

        # 选邻居函数：down=successors / up=predecessors
        neighbors = graph.successors if direction == "down" else graph.predecessors

        # BFS 闭包：visited 不含起点；按 max_depth / max_nodes 双重封顶
        try:
            visited: set[str] = set()
            # 队列元素 = (node, depth)；起点 depth 0
            queue: deque[tuple[str, int]] = deque([(entity_id, 0)])
            seen = {entity_id}
            while queue:
                node, depth = queue.popleft()
                if depth >= max_depth:
                    continue
                for nxt in neighbors(node):
                    if nxt in seen:
                        continue
                    seen.add(nxt)
                    visited.add(nxt)
                    if len(visited) >= _DEFAULT_MAX_NODES:
                        break
                    queue.append((nxt, depth + 1))
                if len(visited) >= _DEFAULT_MAX_NODES:
                    break
        except Exception as e:
            return {
                "entity_id": entity_id,
                "direction": direction,
                "count": 0,
                "nodes": [],
                "error": f"graph backend error: {e}",
            }

        nodes = sorted(visited)
        return {
            "entity_id": entity_id,
            "direction": direction,
            "count": len(nodes),
            "nodes": nodes,
        }

    return Tool(
        name="ke_impact",
        description=(
            "影响分析：给一个代码实体（method/class），沿调用关系做多跳 BFS 闭包。"
            "direction=down 求下游影响面（改它会波及谁）；up 求上游依赖面（谁依赖它）。"
        ),
        input_schema=_KE_IMPACT_SCHEMA,
        handler=handler,
    )
