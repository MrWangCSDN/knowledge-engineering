"""
展示层数值常量：进度块数、自动刷新间隔、内联布局等。

主题色与大段 CSS 仍在 ``styles.GLOBAL_CSS``；此处仅收拢 Python / 内联 HTML 中的「可调参数」。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DisplayTheme:
    """流水线进度、清单窗口、Fragment 刷新等 UI 参数。"""

    # 方块进度条块数（与 pipeline_runner 中 █ 条视觉密度可分别调）
    progress_blocks_count: int = 25
    # 文本进度条（markdown）段数：解读线程 _p_prog 与完成态
    text_progress_bar_segments: int = 20
    # 全量流水线完成时 100% 条用的段数（历史为 25，与方块数对齐）
    full_pipeline_complete_bar_segments: int = 25
    # 解读清单滑动窗口长度
    checklist_window_size: int = 25
    # 主内容区 / 侧边栏诊断 fragment 刷新间隔（秒）
    fragment_refresh_interval_sec: int = 3
    # 技术/业务清单之间的垂直留白（内联 HTML）
    checklist_section_spacer_height: str = "2.6em"
    # 侧边栏诊断统计块上边距
    diag_stats_block_margin_top_first: str = "1.15rem"
    diag_stats_block_margin_top_second: str = "0.85rem"


# 默认主题单例（模块级常量，便于 ``from src.app.ui.display_theme import DISPLAY_THEME``）
DISPLAY_THEME = DisplayTheme()
