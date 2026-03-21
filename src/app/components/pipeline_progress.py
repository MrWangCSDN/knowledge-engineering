"""流水线进度与状态渲染组件。"""
from __future__ import annotations

import html
from typing import Optional

import streamlit as st

from src.app.i18n.ui_strings import get_ui_strings
from src.app.ui.display_theme import DISPLAY_THEME
from src.app.ui.streamlit_keys import SessionKeys
from src.core.domain_enums import InterpretPhase

_PT = InterpretPhase.TECH.value
_PB = InterpretPhase.BIZ.value


def _checklist_window(
    checklist: list,
    done_hint: int,
    *,
    window: int | None = None,
) -> tuple[list, int, int]:
    """
    在清单上取滑动窗口。done_hint 可能大于 len(checklist)（清单仅为本轮子集时），
    此时固定显示清单末尾 window 条，避免出现「显示第 158–144/144」这类非法区间。
    """
    win = int(window if window is not None else DISPLAY_THEME.checklist_window_size)
    n = len(checklist)
    if n == 0:
        return [], 0, 0
    if done_hint >= n:
        start = max(0, n - win)
    else:
        start = max(0, done_hint - 1)
    end = min(n, start + win)
    if start >= end:
        start = max(0, max(0, n - win))
        end = min(n, start + win)
    return checklist[start:end], start, end


class PipelineProgressRenderer:
    """全量构建与仅运行解读共用的进度/状态呈现。"""

    @staticmethod
    def render(
        status: str,
        *,
        progress_frac: Optional[float] = None,
        progress_md: Optional[str] = None,
        progress_label: Optional[str] = None,
        stats_md: Optional[str] = None,
        checklist_tech: Optional[list] = None,
        checklist_biz: Optional[list] = None,
        interp_stats: Optional[dict] = None,
        checklist_md: Optional[str] = None,
        steps: Optional[list] = None,
        progress_source: Optional[str] = None,
        llm_backend_info: Optional[str] = None,
        header_caption: Optional[str] = None,
        show_divider: bool = True,
        show_refresh_button: bool = False,
        interpret_current_tech: Optional[str] = None,
        interpret_current_biz: Optional[str] = None,
    ) -> None:
        _S = get_ui_strings()
        _pp = (_S.get("pipeline_progress") or {}) if isinstance(_S, dict) else {}

        def _chk(d: bool, label: str, current: Optional[str]) -> str:
            return "[x]" if d or (current and label == current) else "[ ]"
        def _render_phase_progress(title: str, done: int, total: int) -> None:
            display_done = min(done, total) if total > 0 else done
            _tpl_run = str(_pp.get("phase_running_label") or "{title}：{done}/{total}")
            label_text = _tpl_run.format(title=title, done=display_done, total=total)
            if total > 0 and done >= total:
                _tpl_done = str(_pp.get("phase_done_label") or "已完成（{total}/{total}）")
                label_text = f"{title}：" + _tpl_done.format(total=total)
            st.markdown(
                f'<p class="interpret-progress-label">{html.escape(label_text)}</p>',
                unsafe_allow_html=True,
            )
            frac = min(display_done / total, 1.0) if total else 0.0
            _n_blocks = DISPLAY_THEME.progress_blocks_count
            _filled = int(round(frac * _n_blocks))
            _blocks_html = "".join(
                f'<span class="block filled"></span>' if i < _filled else '<span class="block"></span>'
                for i in range(_n_blocks)
            )
            st.markdown(
                f'<div class="progress-blocks">{_blocks_html}</div>',
                unsafe_allow_html=True,
            )

        _istats = interp_stats or {}
        _has_tech = _PT in _istats and isinstance(_istats.get(_PT), (list, tuple)) and len(_istats.get(_PT)) == 2
        _has_biz = _PB in _istats and isinstance(_istats.get(_PB), (list, tuple)) and len(_istats.get(_PB)) == 2
        _show_dual_phase_bars = _has_tech and _has_biz

        checklist_tech = checklist_tech or []
        checklist_biz = checklist_biz or []

        if header_caption:
            st.caption(header_caption)
        if show_divider:
            st.markdown("---")
        st.markdown(f"### **{html.escape(status)}**")

        # 技术+业务双阶段：不显示顶部「主进度条 / progress_md / stats / 来源 / LLM」，只保留下方方块条
        if not _show_dual_phase_bars:
            if progress_frac is not None:
                _label = progress_label or str(_pp.get("default_progress_label") or "进度")
                st.markdown(
                    f'<p class="interpret-progress-label">{html.escape(_label)}</p>',
                    unsafe_allow_html=True,
                )
                _n_blocks = DISPLAY_THEME.progress_blocks_count
                _filled = int(round(progress_frac * _n_blocks))
                _blocks_html = "".join(
                    f'<span class="block filled"></span>' if i < _filled else '<span class="block"></span>'
                    for i in range(_n_blocks)
                )
                st.markdown(
                    f'<div class="progress-blocks">{_blocks_html}</div>',
                    unsafe_allow_html=True,
                )
            elif progress_md:
                st.markdown(progress_md)
            if stats_md and progress_frac is None:
                st.markdown(stats_md)
            if progress_source:
                st.caption(
                    f'{str(_pp.get("progress_source_prefix") or "进度来源")}{progress_source}'
                )
            if llm_backend_info:
                st.caption(
                    f'{str(_pp.get("llm_backend_prefix") or "LLM 后端：")}{llm_backend_info}'
                )

        if _show_dual_phase_bars:
            td, tt = _istats[_PT]
            bd, bt = _istats[_PB]
            td, tt, bd, bt = int(td), int(tt), int(bd), int(bt)

            # 1) 技术解读：仅方块进度条 + 标题文案
            _render_phase_progress(
                str(_pp.get("tech_phase_title") or "技术解读进度"),
                td,
                tt,
            )

            # 2) 技术解读清单（加粗）：紧接在技术条下方、业务条上方
            _safe = lambda x: html.escape(x).replace("`", "&#96;")
            # td/tt 达到完成时：不展示清单标题与长清单（避免“100% 仍显示待解读”）
            tech_done = (tt > 0 and td >= tt)
            if checklist_tech and not tech_done:
                st.markdown(str(_pp.get("tech_checklist_heading") or "**技术解读清单**"))
                if not tech_done and checklist_tech:
                    done_count = td if tt else sum(1 for _, d in checklist_tech if d)
                    _windowed, _, _ = _checklist_window(checklist_tech, done_count)
                    _lines = [
                        f"- {_chk(d, s, interpret_current_tech)} `{_safe(s)}`" for s, d in _windowed
                    ]
                    st.markdown("\n".join(_lines))

            # 技术清单与业务进度之间留白（约 2 行）
            st.markdown(
                f'<div aria-hidden="true" style="height:{DISPLAY_THEME.checklist_section_spacer_height};line-height:1.3;">&nbsp;</div>',
                unsafe_allow_html=True,
            )

            # 3) 业务解读：方块进度条
            _render_phase_progress(
                str(_pp.get("biz_phase_title") or "业务解读进度"),
                bd,
                bt,
            )

            # 4) 业务解读清单：done 可能大于本轮清单长度，窗口钳制到清单末尾
            biz_done = (bt > 0 and bd >= bt)
            if checklist_biz and not biz_done:
                _biz_done, _biz_total = bd, bt
                done_biz = _biz_done if _biz_total else sum(1 for _, d in checklist_biz if d)
                _windowed, _, _ = _checklist_window(checklist_biz, done_biz)
                _lines = [
                    f"- {_chk(d, s, interpret_current_biz)} `{_safe(s)}`" for s, d in _windowed
                ]
                st.markdown(str(_pp.get("biz_checklist_heading") or "**业务解读清单**"))
                st.markdown("\n".join(_lines))

        elif checklist_md:
            st.markdown(checklist_md)
        elif checklist_tech or checklist_biz:
            _safe = lambda x: html.escape(x).replace("`", "&#96;")
            _parts = []
            if checklist_tech:
                _tech_done, _tech_total = _istats.get(_PT, (0, 0))
                tech_done2 = (_tech_total > 0 and _tech_done >= _tech_total)
                if not tech_done2:
                    _parts.append(str(_pp.get("tech_checklist_heading") or "**技术解读清单**"))
                    done_count = _tech_done if _tech_total else sum(1 for _, d in checklist_tech if d)
                    _windowed, _, _ = _checklist_window(checklist_tech, done_count)
                    _lines = [
                        f"- {_chk(d, s, interpret_current_tech)} `{_safe(s)}`" for s, d in _windowed
                    ]
                    _parts.append("\n".join(_lines))
            if checklist_biz:
                _biz_done, _biz_total = _istats.get(_PB, (0, 0))
                done_biz = _biz_done if _biz_total else sum(1 for _, d in checklist_biz if d)
                biz_done2 = (_biz_total > 0 and _biz_done >= _biz_total)
                if not biz_done2:
                    _windowed, _, _ = _checklist_window(checklist_biz, done_biz)
                    _lines = [
                        f"- {_chk(d, s, interpret_current_biz)} `{_safe(s)}`" for s, d in _windowed
                    ]
                    _parts.append(str(_pp.get("biz_checklist_heading") or "**业务解读清单**"))
                    _parts.append("\n".join(_lines))
            st.markdown("  \n\n".join(_parts))

        st.markdown(str(_pp.get("steps_heading") or "**工序步骤**"))
        for line in steps or []:
            st.caption(f"· {html.escape(line)}")
        if show_refresh_button:
            if st.button(
                str(_pp.get("refresh_button") or "🔄 刷新进度"),
                key=SessionKeys.REFRESH_PIPELINE_PROGRESS,
            ):
                st.rerun()
