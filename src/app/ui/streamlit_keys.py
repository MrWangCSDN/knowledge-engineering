"""
Streamlit session_state / widget key 命名空间。

统一前缀 ``ke_``，多页面嵌入时可整体改 ``NS``，并避免与第三方组件键冲突。
注意：修改 ``NS`` 后用户已打开的会话中旧键将不再读取（需刷新页面）。
"""
from __future__ import annotations


class SessionKeys:
    """应用级会话键（含部分与 widget key= 共用）。"""

    NS = "ke"

    # --- 应用单例 ---
    APP_SERVICES = f"{NS}_app_services"

    # --- 流水线上次结果快照 ---
    PIPELINE_LAST_STEPS = f"{NS}_pipeline_last_steps"
    PIPELINE_LAST_STATUS = f"{NS}_pipeline_last_status"
    PIPELINE_LAST_PROGRESS_MD = f"{NS}_pipeline_last_progress_md"
    PIPELINE_LAST_STATS_MD = f"{NS}_pipeline_last_stats_md"
    PIPELINE_LAST_INTERP_STATS = f"{NS}_pipeline_last_interp_stats"
    PIPELINE_LAST_CHECKLIST_MD = f"{NS}_pipeline_last_checklist_md"

    # --- Step2 缓存：避免重复遍历大量图谱节点 ---
    STEP2_TYPE_COUNTS_ROWS_CACHE = f"{NS}_step2_type_counts_rows_cache"
    STEP2_TYPE_COUNTS_CACHE_GRAPH_SIG = f"{NS}_step2_type_counts_cache_graph_sig"

    # --- OWL 补全效果一览懒加载 ---
    OWL_BENEFIT_LOADED = f"{NS}_owl_benefit_loaded"
    OWL_BENEFIT_LOAD_BTN = f"{NS}_owl_benefit_load_btn"

    # --- 模式识别：Weaviate 已存结果浏览（session 缓存，避免每轮重复查询）---
    PATTERN_WEAVIATE_BROWSE_CACHE = f"{NS}_pattern_weaviate_browse_cache"
    PATTERN_BROWSE_LOAD_BTN = f"{NS}_pattern_browse_load_btn"
    PATTERN_BROWSE_CLEAR_BTN = f"{NS}_pattern_browse_clear_btn"

    # --- 运行中标记（后台线程结束时 pop）---
    INTERPRET_PIPELINE_RUNNING = f"{NS}_interpret_pipeline_running"
    FULL_PIPELINE_RUNNING = f"{NS}_full_pipeline_running"

    # --- 侧边栏：配置路径（与主内容区预览共用）---
    CONFIG_PATH = f"{NS}_config_path"

    # --- 仅解读 expander ---
    INTERPRET_ONLY_STRUCTURE_FACTS_PATH = f"{NS}_interpret_only_structure_facts_path"
    INTERPRET_ONLY_TECH = f"{NS}_interpret_only_tech"
    INTERPRET_ONLY_BIZ = f"{NS}_interpret_only_biz"
    INTERPRET_ONLY_SHOW_DIAG = f"{NS}_interpret_only_show_diag"
    BTN_INTERPRET_ONLY = f"{NS}_btn_interpret_only"

    # --- 全量流水线选项 ---
    SIDEBAR_PIPELINE_INCLUDE_INTERPRETATION = f"{NS}_sidebar_pipeline_include_interpretation"
    SIDEBAR_PIPELINE_INCLUDE_BUSINESS = f"{NS}_sidebar_pipeline_include_business"

    # --- 主步骤导航 ---
    MAIN_STEP = f"{NS}_main_step"

    # --- 首页展示模式：流程步骤 / 场景样板间 ---
    MAIN_HOME_MODE = f"{NS}_main_home_mode"

    # --- 流水线进度区 ---
    REFRESH_PIPELINE_PROGRESS = f"{NS}_refresh_pipeline_progress"

    # --- OWL / 推理 ---
    OWL_EXPORT = f"{NS}_owl_export"
    OWL_WRITE_BACK = f"{NS}_owl_write_back"
    OWL_REASONER = f"{NS}_owl_reasoner"
    OWL_RUN = f"{NS}_owl_run"
    OWL_LAST_RESULT = f"{NS}_owl_last_result"
    OWL_DO_COMPARE = f"{NS}_owl_do_compare"
    OWL_COMPARE_PREFILL = f"{NS}_owl_compare_prefill"
    OWL_COMPARE_ENTITY_ID = f"{NS}_owl_compare_entity_id"
    OWL_COMPARE_DIR = f"{NS}_owl_compare_dir"
    OWL_COMPARE_BTN = f"{NS}_owl_compare_btn"
    OWL_NEO_ENTITY_ID = f"{NS}_owl_neo_entity_id"
    OWL_NEO_DIR = f"{NS}_owl_neo_dir"
    OWL_NEO_BTN = f"{NS}_owl_neo_btn"

    # --- 业务域中心图 ---
    BD_CENTER_SELECTED_DOMAIN = f"{NS}_bd_center_selected_domain"
    BD_CENTER_CAP_LIMIT = f"{NS}_bd_center_cap_limit"
    BD_CENTER_CODE_LIMIT = f"{NS}_bd_center_code_limit"
    BD_CENTER_SELECTED_NODE = f"{NS}_bd_center_selected_node"
    BD_CENTER_SELECTED_NODE_TYPE = f"{NS}_bd_center_selected_node_type"

    # --- 影响分析 / 搜索 ---
    SEARCH_Q = f"{NS}_search_q"
    SEARCH_TYPE = f"{NS}_search_type"
    SELECTED_ENTITY_ID = f"{NS}_selected_entity_id"
    IMPACT_ID = f"{NS}_impact_id"
    IMPACT_DIR = f"{NS}_impact_dir"
    IMPACT_DEPTH = f"{NS}_impact_depth"
    IMPACT_MODE = f"{NS}_impact_mode"

    # --- 本体浏览详情 ---
    ONTOLOGY_DETAIL_NID = f"{NS}_ontology_detail_nid"
    ONTOLOGY_DETAIL_KEY_PREFIX = f"{NS}_ontology_detail_key_prefix"

    @staticmethod
    def stepper_button(step_index: int) -> str:
        return f"{SessionKeys.NS}_stepper_btn_{step_index}"

    @staticmethod
    def owl_use_entity_button(index: int) -> str:
        return f"{SessionKeys.NS}_owl_use_entity_{index}"

    @staticmethod
    def search_use_hit_button(index: int) -> str:
        return f"{SessionKeys.NS}_search_use_{index}"

    @staticmethod
    def relation_row_button(key_prefix: str, index: int) -> str:
        return f"{SessionKeys.NS}_rel_{key_prefix}_{index}"

    @staticmethod
    def ontology_close_button(key_prefix: str) -> str:
        return f"{SessionKeys.NS}_ontology_close_{key_prefix}"
