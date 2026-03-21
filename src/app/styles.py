"""全局样式：进度条、背景、卡片、指标等。

与「方块进度条块数、清单窗口、Fragment 刷新间隔」等 Python 侧展示参数对应关系见
``src.app.ui.display_theme.DisplayTheme`` / ``DISPLAY_THEME``。
"""
import streamlit as st

GLOBAL_CSS = """
<style>
/* 主区背景：纯色，与内部块统一 */
/* 顶部留白加大，避免首屏 h1（如「代码知识工程」）上沿被工具栏/容器裁切 */
.block-container {
    padding-top: 2.75rem;
    padding-bottom: 3rem;
    background: #ffffff !important;
    border-radius: 0;
    min-height: 100vh;
}
[data-testid="stAppViewContainer"] {
    background: #ffffff !important;
}
[data-testid="stHeader"] {
    background: #ffffff !important;
}
main [data-testid="element-container"],
main [data-testid="stVerticalBlock"],
main [data-testid="stVerticalBlockBorderWrapper"] {
    background: transparent !important;
}
/* 进度条 */
[data-testid="stProgress"] > div > div {
    background: linear-gradient(90deg, #059669 0%, #10b981 50%, #34d399 100%) !important;
    border-radius: 6px !important;
    box-shadow: 0 0 8px rgba(5, 150, 105, 0.35), inset 0 1px 0 rgba(255,255,255,0.25) !important;
    transition: width 0.4s ease-out;
}
[data-testid="stProgress"] > div {
    background-color: #e5e7eb !important;
    border-radius: 6px !important;
    overflow: hidden;
    box-shadow: inset 0 1px 2px rgba(0,0,0,0.08);
}
[data-testid="stSidebar"] [data-testid="stProgress"] {
    margin-top: 0.25rem !important;
}
.interpret-progress-label {
    color: #34d399 !important;
    font-weight: 700 !important;
    font-size: 0.95rem !important;
    margin: 0 0 0.35rem 0 !important;
}
.progress-blocks {
    display: flex;
    gap: 3px;
    flex-wrap: wrap;
    margin: 0.4rem 0;
}
.progress-blocks .block {
    width: 12px;
    height: 12px;
    border-radius: 2px;
    background: #e5e7eb;
    transition: background 0.2s;
}
.progress-blocks .block.filled {
    background: linear-gradient(135deg, #059669, #10b981);
    box-shadow: 0 0 4px rgba(5, 150, 105, 0.4);
}
/* 指标卡片 */
[data-testid="stMetric"] {
    background: linear-gradient(135deg, rgba(255,255,255,0.9) 0%, rgba(255,255,255,0.7) 100%);
    padding: 1rem 1rem 0.8rem !important;
    border-radius: 10px !important;
    border: 1px solid rgba(255,255,255,0.8);
    box-shadow: 0 4px 14px rgba(0,0,0,0.06), 0 1px 3px rgba(0,0,0,0.04);
}
[data-testid="stMetricValue"] {
    color: #4338ca !important;
    font-weight: 700 !important;
}
/* 按钮 */
.stButton > button[kind="primary"] {
    background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%) !important;
    color: white !important;
    border: none !important;
    border-radius: 8px !important;
    box-shadow: 0 4px 12px rgba(99, 102, 241, 0.35) !important;
    font-weight: 600 !important;
    transition: transform 0.2s, box-shadow 0.2s !important;
}
.stButton > button[kind="primary"]:hover {
    transform: translateY(-1px);
    box-shadow: 0 6px 16px rgba(99, 102, 241, 0.45) !important;
}
.stButton > button {
    border-radius: 8px !important;
    font-weight: 500 !important;
}
/* 标题 */
h1, h2, h3 {
    font-weight: 700 !important;
    color: #1e293b !important;
    letter-spacing: -0.02em;
}
h1 {
    border-bottom: 2px solid rgba(99, 102, 241, 0.3);
    padding-bottom: 0.3em !important;
    padding-top: 0.2em !important;
    line-height: 1.35 !important;
}
/* 主内容区第一个标题再留一点顶距（与 block-container 叠加，避免字形被切） */
.main .block-container > div:first-child h1,
section.main h1:first-of-type {
    margin-top: 0.25rem !important;
}
/* 数据框 */
[data-testid="stDataFrame"] {
    border-radius: 10px !important;
    overflow: hidden;
    box-shadow: 0 2px 8px rgba(0,0,0,0.06);
}
.stTextInput > div > div input, .stSelectbox > div > div {
    border-radius: 8px !important;
}
.stSuccess, .stInfo, .stWarning {
    border-radius: 10px !important;
    border-left: 4px solid #6366f1 !important;
}
/* 侧边栏 */
[data-testid="stSidebar"] {
    background: #ffffff !important;
}
[data-testid="stSidebar"] .stButton > button[kind="primary"] {
    background: linear-gradient(135deg, #6366f1 0%, #7c3aed 100%) !important;
}
/* 扩展器 */
[data-testid="stExpander"] {
    margin: 0.75rem 0 !important;
    border: 1px solid rgba(0,0,0,0.08) !important;
    border-radius: 10px !important;
    background: #fafbfc !important;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04) !important;
}
[data-testid="stExpander"] details {
    border: none !important;
    background: transparent !important;
    padding: 1.25rem 1.5rem !important;
}
.step-label {
    text-align: center !important;
    font-size: 1.05rem !important;
    font-weight: 500 !important;
    margin-top: 0.4rem !important;
    margin-bottom: 0 !important;
}
h4, .element-container h4 {
    margin-top: 1.35rem !important;
    margin-bottom: 0.6rem !important;
}
h4:first-of-type { margin-top: 0.5rem !important; }

/* Radio（单选）文字：在首页“主页模式”需要更醒目 */
.stRadio label,
[data-testid="stRadio"] label {
    font-size: 1.25rem !important;
    font-weight: 800 !important;
}
.stRadio label > div,
[data-testid="stRadio"] label > div {
    font-size: 1.25rem !important;
    font-weight: 800 !important;
}
</style>
"""


def inject_global_styles() -> None:
    """注入全局 CSS 样式。"""
    st.markdown(GLOBAL_CSS, unsafe_allow_html=True)
