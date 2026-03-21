"""业务总览 / 模块视图。"""
from __future__ import annotations

from pathlib import Path

import streamlit as st

from src.core.domain_enums import BusinessInterpretLevel
from src.core.weaviate_defaults import (
    DEFAULT_COLLECTION_BUSINESS_INTERPRETATION,
    DEFAULT_WEAVIATE_GRPC_PORT,
    DEFAULT_WEAVIATE_HTTP_URL,
)
from src.knowledge.weaviate_business_store import WeaviateBusinessInterpretStore


class BusinessOverviewView:
    """模块级业务解读列表。"""

    def __init__(self, load_config_fn, root: Path):
        self._load_config = load_config_fn
        self._root = root

    def render(self) -> None:
        """渲染模块级业务综述。"""
        st.markdown("##### 模块级业务综述")
        st.caption(
            "以下数据来自 Weaviate `BusinessInterpretation` collection 的 `level=module` 业务解读。"
        )
        try:
            raw = self._load_config(str(self._root / "config/project.yaml"))
            cfg = raw.model_dump() if hasattr(raw, "model_dump") else (raw or {})
            bcfg = (cfg.get("knowledge") or {}).get("vectordb-business") or {}
            if not (bcfg.get("enabled") and bcfg.get("backend") == "weaviate"):
                st.info(
                    "尚未启用 vectordb-business（knowledge.vectordb-business），"
                    "无法展示模块级业务解读。"
                )
                return
            store = WeaviateBusinessInterpretStore(
                url=bcfg.get("weaviate_url") or DEFAULT_WEAVIATE_HTTP_URL,
                grpc_port=int(bcfg.get("weaviate_grpc_port") or DEFAULT_WEAVIATE_GRPC_PORT),
                collection_name=bcfg.get("collection_name") or DEFAULT_COLLECTION_BUSINESS_INTERPRETATION,
                dimension=int(bcfg.get("dimension") or 1024),
                api_key=bcfg.get("weaviate_api_key"),
            )
        except Exception as e:
            st.warning(f"读取业务解读向量库配置或连接 Weaviate 失败：{e!r}")
            return

        modules = []
        try:
            modules = store.list_by_level(BusinessInterpretLevel.MODULE.value, limit=200)
        except Exception:
            modules = []
        finally:
            try:
                store.close()
            except Exception:
                pass

        if not modules:
            st.info(
                "当前 BusinessInterpretation 中尚无模块级业务解读记录。"
                "请确认 `business_interpretation.enabled: true` 且已成功运行流水线。"
            )
            return

        domains = sorted(
            {(m.get("business_domain") or "").strip() for m in modules if m.get("business_domain")}
        )
        cols = st.columns([2, 2, 1])
        with cols[0]:
            dom_filter = st.selectbox(
                "按业务域筛选",
                options=["（全部业务域）"] + domains,
                key="biz_overview_domain",
            )
        with cols[1]:
            kw = st.text_input(
                "按模块 ID 关键词筛选",
                placeholder="输入模块 ID 片段，如 mall-order",
                key="biz_overview_kw",
            )
        with cols[2]:
            st.caption(f"共 {len(modules)} 条")

        def _match(m: dict) -> bool:
            if dom_filter != "（全部业务域）" and (m.get("business_domain") or "").strip() != dom_filter:
                return False
            if kw:
                mid = (m.get("entity_id") or "").lower()
                if kw.lower() not in mid:
                    return False
            return True

        filtered = [m for m in modules if _match(m)]
        if not filtered:
            st.info("无匹配的模块业务解读，请调整筛选条件。")
            return

        table_rows = [
            {
                "模块 ID": m.get("entity_id") or "",
                "业务域": m.get("business_domain") or "",
                "能力标签": m.get("business_capabilities") or "",
            }
            for m in filtered
        ]
        st.dataframe(table_rows, use_container_width=True, hide_index=True)

        st.markdown("##### 模块业务解读详情")
        for m in filtered:
            mid = m.get("entity_id") or ""
            title = f"{mid}（{m.get('business_domain') or '未标注业务域'}）"
            closed_key = f"biz_overview_exp_{mid}_force_closed"
            # 仅在点击“关闭详情”后的那一次渲染中强制折叠；
            # pop 后会立刻清除该标记，保证下一次用户仍可正常点开详情。
            force_closed = bool(st.session_state.pop(closed_key, False))

            if force_closed:
                exp_ctx = st.expander(title, expanded=False)
            else:
                exp_ctx = st.expander(title)

            with exp_ctx:
                caps = m.get("business_capabilities") or ""
                if caps:
                    st.caption(f"能力标签：{caps}")
                if st.button("关闭详情", key=f"biz_overview_close_{mid}"):
                    # 折叠当前 expander，并触发 rerun 让列表区域重新处于可见状态。
                    st.session_state[closed_key] = True
                    st.rerun()
                st.markdown(m.get("summary_text") or "")
