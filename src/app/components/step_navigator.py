"""步骤导航器：糖葫芦串式步骤选择。"""
from __future__ import annotations

import html
from typing import Sequence

import streamlit as st

from src.app.i18n.ui_strings import step_navigator_tuples
from src.app.ui.streamlit_keys import SessionKeys


class StepNavigator:
    """糖葫芦串步骤器：①—②—③—④—⑤ 横向可点击。"""

    DEFAULT_STEPS = [
        ("step1", "①", "接入代码 & 构建图谱"),
        ("step2", "②", "图谱统计 / 系统结构"),
        ("step3", "③", "理解代码 & 评估影响"),
        ("step4", "④", "智能推理：补全间接依赖"),
        ("step5", "⑤", "模式识别：设计模式与架构模式"),
        ("step6", "⑥", "业务总览 / 模块视图"),
    ]

    def __init__(
        self,
        steps: Sequence[tuple[str, str, str]] | None = None,
        session_key: str = SessionKeys.MAIN_STEP,
        default_step: str = "step1",
    ):
        _loaded = step_navigator_tuples()
        self._steps = list(steps) if steps is not None else (_loaded if _loaded else list(self.DEFAULT_STEPS))
        self._session_key = session_key
        self._default_step = default_step

    def ensure_session(self) -> None:
        if self._session_key not in st.session_state:
            st.session_state[self._session_key] = self._default_step

    def render(self) -> str:
        """渲染步骤器，返回当前选中的 step_key。"""
        self.ensure_session()
        cols_spec: list[float] = []
        for idx in range(len(self._steps)):
            cols_spec.append(1.0)
            if idx < len(self._steps) - 1:
                cols_spec.append(0.2)
        cols = st.columns(cols_spec)
        current = st.session_state[self._session_key]
        for i, (step_key, num, label) in enumerate(self._steps):
            bead_col = cols[i * 2]
            with bead_col:
                is_active = current == step_key
                if st.button(
                    num,
                    key=SessionKeys.stepper_button(i),
                    type="primary" if is_active else "secondary",
                    use_container_width=True,
                ):
                    st.session_state[self._session_key] = step_key
                    st.rerun()
                st.markdown(
                    f'<p class="step-label">{html.escape(label)}</p>',
                    unsafe_allow_html=True,
                )
            if i < len(self._steps) - 1:
                with cols[i * 2 + 1]:
                    st.markdown(
                        "<div style='text-align:center; color:#ccc; margin-bottom:1.4rem;'>—</div>",
                        unsafe_allow_html=True,
                    )
        return st.session_state[self._session_key]
