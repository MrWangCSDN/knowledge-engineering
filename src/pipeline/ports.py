"""流水线对外契约（Protocol）：应用 / API 层可只依赖本模块类型，便于测试替换实现。"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Protocol

from src.config import ProjectConfig


class ProjectConfigLoader(Protocol):
    """从 YAML 路径加载 ``ProjectConfig``。"""

    def __call__(self, config_path: str | Path) -> ProjectConfig: ...


class InterpretationProgressProvider(Protocol):
    """查询 Weaviate 解读进度（done/total）。"""

    def __call__(
        self,
        config_path: str | Path,
        structure_facts_json: Optional[str | Path] = None,
    ) -> dict[str, dict[str, int]]: ...


