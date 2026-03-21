"""应用层 / 薄服务推荐入口：配置加载与解读进度查询。

避免 ``streamlit`` / ``services`` 直接依赖 ``run`` 聚合模块或 ``stage_runtime``，
依赖方向：``app`` → ``gateways`` → ``config_bootstrap`` / ``interpretation_standalone``。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from src.config import ProjectConfig
from src.pipeline.config_bootstrap import load_config
from src.pipeline.interpretation_standalone import get_interpretation_progress_from_weaviate


def load_project_config(config_path: str | Path) -> ProjectConfig:
    """加载项目 YAML 为 ``ProjectConfig``（与 ``run.load_config`` 等价）。"""
    return load_config(config_path)


def get_interpretation_progress(
    config_path: str | Path,
    structure_facts_json: Optional[str | Path] = None,
) -> dict[str, dict[str, int]]:
    """从 Weaviate 查询解读进度；与 ``run.get_interpretation_progress_from_weaviate`` 等价。"""
    return get_interpretation_progress_from_weaviate(
        config_path, structure_facts_json=structure_facts_json
    )


__all__ = [
    "load_project_config",
    "get_interpretation_progress",
]
