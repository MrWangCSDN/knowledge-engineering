"""
项目路径与输出目录约定（单一来源）。

UI、持久化、流水线应从此模块取路径，避免在业务代码中散落 ``out_ui/`` 等字面量。
"""
from __future__ import annotations

from pathlib import Path

# --- 目录名（相对「项目根」或父目录） ---
OUT_UI_DIR_NAME = "out_ui"
"""与源码同级的输出目录，存放解读进度、结构事实缓存、UI 用图谱快照等。"""

KNOWLEDGE_SNAPSHOT_DIR_NAME = "knowledge_snapshot"
"""图谱快照子目录名：既用于 ``out_ui/`` 下 UI 快照，也用于流水线 ``out_dir`` 下快照。"""

# --- 文件名 ---
STRUCTURE_FACTS_INTERPRET_CACHE_FILENAME = "structure_facts_for_interpret.json"
"""完整流水线/解读默认读取的结构事实缓存文件名。"""

INTERPRETATION_PROGRESS_FILENAME = "interpretation_progress.json"
"""按 structure_facts 路径记录解读进度的 JSON 文件名。"""


def project_root_from_config(config_path: Path | str) -> Path:
    """约定配置文件位于 ``<项目根>/config/*.yaml`` 时，由 config 路径解析项目根。"""
    return Path(config_path).resolve().parent.parent


def out_ui_dir(project_root: Path) -> Path:
    return project_root / OUT_UI_DIR_NAME


def structure_facts_interpret_cache_path(project_root: Path) -> Path:
    """``<项目根>/out_ui/structure_facts_for_interpret.json``"""
    return out_ui_dir(project_root) / STRUCTURE_FACTS_INTERPRET_CACHE_FILENAME


def structure_facts_interpret_cache_path_from_config(config_path: Path | str) -> Path:
    """由 ``config/project.yaml`` 等路径得到默认结构事实缓存绝对路径。"""
    return structure_facts_interpret_cache_path(project_root_from_config(config_path))


def structure_facts_interpret_cache_display_path() -> str:
    """用于用户提示文案的相对路径，如 ``out_ui/structure_facts_for_interpret.json``。"""
    return f"{OUT_UI_DIR_NAME}/{STRUCTURE_FACTS_INTERPRET_CACHE_FILENAME}"


def interpretation_progress_path(project_root: Path) -> Path:
    """解读进度汇总文件：``<项目根>/out_ui/interpretation_progress.json``。"""
    return out_ui_dir(project_root) / INTERPRETATION_PROGRESS_FILENAME


def ui_knowledge_snapshot_dir(project_root: Path) -> Path:
    """Streamlit 等 UI 加载/保存内存图快照的目录：``<项目根>/out_ui/knowledge_snapshot``。"""
    return out_ui_dir(project_root) / KNOWLEDGE_SNAPSHOT_DIR_NAME


def pipeline_output_knowledge_snapshot_dir(output_dir: Path) -> Path:
    """流水线在指定 ``out_dir`` 下写入图谱快照的目录（与 UI 目录可不同）。"""
    return Path(output_dir) / KNOWLEDGE_SNAPSHOT_DIR_NAME
