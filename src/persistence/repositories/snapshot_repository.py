from __future__ import annotations

from pathlib import Path
from typing import Optional, Protocol, Union

from src.knowledge import KnowledgeGraph

PathLike = Union[str, Path]


class SnapshotRepository(Protocol):
    """知识图谱快照仓储接口（可替换存储后端）。"""

    def save(self, graph: KnowledgeGraph, snapshot_dir: PathLike, version: str = "default") -> Path:
        """将当前图与元数据保存为快照目录，并返回目录路径。"""

    def load(self, graph: KnowledgeGraph, snapshot_dir: PathLike) -> None:
        """从快照目录加载图，覆盖当前图。"""


class GraphSnapshotRepository(SnapshotRepository):
    """文件系统后端：委托给 `KnowledgeGraph.save_snapshot/load_snapshot`。"""

    def save(self, graph: KnowledgeGraph, snapshot_dir: PathLike, version: str = "default") -> Path:
        return graph.save_snapshot(snapshot_dir, version=version)

    def load(self, graph: KnowledgeGraph, snapshot_dir: PathLike) -> None:
        graph.load_snapshot(snapshot_dir)

