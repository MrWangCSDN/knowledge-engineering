"""索引作业状态常量 + 顺序。设计 §9 状态机。"""
QUEUED = "queued"
CLONING = "cloning"
BUILDING_GRAPH = "building_graph"
CROSS_SERVICE = "cross_service"
EMBEDDING = "embedding"
INTERPRETING = "interpreting"
DONE = "done"
FAILED = "failed"

# 工作阶段顺序（不含 queued/done/failed）；indexer 按此上报进度
PHASE_ORDER = [CLONING, BUILDING_GRAPH, CROSS_SERVICE, EMBEDDING, INTERPRETING]

_TERMINAL = {DONE, FAILED}


def is_terminal(status: str) -> bool:
    """作业是否已到终态（done/failed）。"""
    return status in _TERMINAL
