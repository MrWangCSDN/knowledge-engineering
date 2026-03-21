"""代码知识工程 — Streamlit Web 界面入口。"""
from __future__ import annotations

import logging
import os
import sys
import warnings
from pathlib import Path

import streamlit as st

from src.app.services.app_services import AppServices
from src.app.i18n.ui_strings import get_ui_strings
from src.app.styles import inject_global_styles
from src.app.ui.streamlit_keys import SessionKeys
from src.pipeline.gateways import load_project_config

warnings.filterwarnings("ignore", category=DeprecationWarning, message=".*RefResolver.*")
warnings.filterwarnings("ignore", category=DeprecationWarning, module="altair")
warnings.filterwarnings("ignore", category=DeprecationWarning, module="jsonschema")

# 保证项目根在 path 中（main.py 在项目根，app 在 src/app/）
_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

# 降低 TensorFlow / 底层框架的控制台噪音日志
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
os.environ.setdefault("TF_ENABLE_ONEDNN_OPTS", "0")
try:
    import tensorflow as tf  # type: ignore

    tf.get_logger().setLevel("ERROR")
except Exception:
    pass
for noisy in ("tensorflow", "torch", "weaviate"):
    try:
        logging.getLogger(noisy).setLevel(logging.ERROR)
    except Exception:
        pass

st.set_page_config(
    page_title="代码知识工程",
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("📚 代码知识工程")
st.caption("数据与触发 → 结构 → 语义 → 知识层 → 检索/影响分析/图谱")

inject_global_styles()

from src.app.facades.main_content_facade import MainContentFacade
from src.app.facades.sidebar_facade import SidebarFacade
from src.core.context import AppContext
from src.service.api import set_global_config

services = st.session_state.get(SessionKeys.APP_SERVICES)
if not isinstance(services, AppServices):
    services = AppServices(root=_root, load_config_fn=load_project_config)
    st.session_state[SessionKeys.APP_SERVICES] = services

# 未跑流水线时 AppContext 常无配置；get_neo4j_backend_optional 仅靠 cwd 找 YAML 易失败。每次脚本运行若尚无配置则注入仓库根下默认 project.yaml
_cfg_yaml = _root / "config" / "project.yaml"
if AppContext.get().get_config() is None and _cfg_yaml.is_file():
    try:
        _pc = services.load_config_fn(_cfg_yaml)
        set_global_config(_pc.model_dump() if hasattr(_pc, "model_dump") else dict(_pc))
    except Exception:
        pass

SidebarFacade(
    root=_root,
    services=services,
    load_config_fn=load_project_config,
).render()

MainContentFacade(
    root_path=_root,
    services=services,
).render()
