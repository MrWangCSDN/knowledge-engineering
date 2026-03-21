"""模式识别：设计模式与架构模式（system 与 module 级）。"""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from pathlib import Path
from typing import Any

import streamlit as st

from src.app.ui.streamlit_keys import SessionKeys
from src.knowledge.llm import LLMProviderFactory
from src.knowledge.pattern_recognition_runner import recognize_patterns_for_scope
from src.knowledge.weaviate_pattern_store import WeaviatePatternInterpretStore
from src.models.structure import EntityType


class PatternRecognitionView:
    """Step ⑤：模式识别 UI（MVP：LLM + 结构证据上下文，结果落入 Weaviate）。"""

    def __init__(self, *, load_config_fn, services: Any, root: Path):
        self._load_config = load_config_fn
        self._services = services
        self._root = root

    def _resolve_cfg_path(self) -> Path:
        cfg_path_raw = st.session_state.get(SessionKeys.CONFIG_PATH) or "config/project.yaml"
        cfg_path = Path(cfg_path_raw)
        if not cfg_path.is_absolute():
            cfg_path = self._root / cfg_path
        return cfg_path

    def _resolve_structure_facts_path(self) -> Path:
        sf_path_raw = st.session_state.get(SessionKeys.INTERPRET_ONLY_STRUCTURE_FACTS_PATH)
        if not sf_path_raw:
            # 用 services/pipeline 的默认缓存路径：out_ui/structure_facts_for_interpret.json
            return self._services.structure_facts_repo.get_default_cache_path(self._resolve_cfg_path())

        sf_path = Path(str(sf_path_raw))
        if not sf_path.is_absolute():
            sf_path = self._root / sf_path
        return sf_path

    @staticmethod
    def _pick_module_ids(facts) -> list[str]:
        counts: dict[str, int] = defaultdict(int)
        for e in facts.entities:
            if not e.module_id:
                continue
            if e.type in (EntityType.CLASS, EntityType.INTERFACE, EntityType.METHOD):
                counts[e.module_id] += 1
        if not counts:
            return []
        return [k for k, _ in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)]

    @staticmethod
    def _pattern_store_from_vectordb_business(pc: Any) -> tuple[WeaviatePatternInterpretStore | None, str | None]:
        """与运行识别共用：业务向量库 Weaviate 连接参数；模式结果写在独立 collection `PatternInterpretation`。"""
        vcfg = pc.knowledge.vectordb_business
        if not vcfg.enabled or (vcfg.backend or "").strip().lower() != "weaviate":
            return None, (
                "当前配置未启用 vectordb-business（或 backend 不是 weaviate）。"
                "模式识别 MVP 仅支持通过 Weaviate 读写 `PatternInterpretation`。"
            )
        store = WeaviatePatternInterpretStore(
            url=vcfg.weaviate_url,
            grpc_port=int(vcfg.weaviate_grpc_port),
            dimension=int(vcfg.dimension or 1024),
            api_key=vcfg.weaviate_api_key,
        )
        return store, None

    def render(self) -> None:
        st.markdown("##### 设计模式与架构模式识别")
        st.caption(
            "运行识别：读取 `structure_facts_for_interpret.json` 并调用 LLM，结果落入 Weaviate `PatternInterpretation`（可断点续跑）。"
            "已存结果：无需 LLM，直接从 Weaviate 展示上次识别内容。"
        )
        tab_run, tab_saved = st.tabs(["运行识别", "已存结果（Weaviate）"])
        with tab_run:
            self._render_run_tab()
        with tab_saved:
            self._render_browse_weaviate_tab()

    def _render_browse_weaviate_tab(self) -> None:
        """代码未变、已落库时：只读 Weaviate，展示 design/architecture 模式。"""
        st.markdown("##### 已存结果（Weaviate）")
        st.caption(
            "从 `PatternInterpretation` **只读**已保存的模式；**不会调用 LLM**。"
            "若勾选「若已存在结果则跳过」后第二次运行识别会直接跳过，也可在此查看历史结果。"
        )
        cfg_path = self._resolve_cfg_path()
        sf_path = self._resolve_structure_facts_path()
        if not cfg_path.exists():
            st.error(f"配置文件不存在：`{cfg_path}`")
            return

        top_n = st.number_input(
            "每个 scope 展示条数上限（按置信度降序截取）",
            min_value=8,
            max_value=100,
            value=25,
            step=1,
            key=f"{SessionKeys.PATTERN_WEAVIATE_BROWSE_CACHE}_top_n",
        )
        enrich_names = st.checkbox(
            "用结构事实 JSON 补全实体显示名（文件存在时）",
            value=True,
            key=f"{SessionKeys.PATTERN_WEAVIATE_BROWSE_CACHE}_enrich",
        )
        max_modules_scan = st.number_input(
            "最多预加载的模块数量",
            min_value=1,
            max_value=80,
            value=24,
            step=1,
            key=f"{SessionKeys.PATTERN_WEAVIATE_BROWSE_CACHE}_max_mod",
        )

        c1, c2 = st.columns(2)
        with c1:
            load_btn = st.button("从 Weaviate 加载/刷新", type="primary", key=SessionKeys.PATTERN_BROWSE_LOAD_BTN)
        with c2:
            if st.button("清除本地浏览缓存", key=SessionKeys.PATTERN_BROWSE_CLEAR_BTN):
                st.session_state.pop(SessionKeys.PATTERN_WEAVIATE_BROWSE_CACHE, None)
                st.rerun()

        cache_key = SessionKeys.PATTERN_WEAVIATE_BROWSE_CACHE

        if load_btn:
            store: WeaviatePatternInterpretStore | None = None
            try:
                pc = self._load_config(str(cfg_path))
                store, err = self._pattern_store_from_vectordb_business(pc)
                if store is None:
                    st.warning(err or "无法连接 Weaviate。")
                    return
                tn = int(top_n)
                raw_sys = store.list_by_scope(scope_type="system", target_id="system", limit=tn * 4)
                raw_sys.sort(key=lambda x: float(x.get("confidence") or 0.0), reverse=True)
                sys_rows = raw_sys[:tn]

                all_mod_ids = sorted(store.list_existing_target_ids("module"))
                cap = int(max_modules_scan)
                mod_ids = all_mod_ids[:cap]
                if len(all_mod_ids) > cap:
                    st.caption(
                        f"提示：Weaviate 中约有 {len(all_mod_ids)} 个模块含数据，本次仅预加载前 {cap} 个（按 target_id 排序）。"
                    )

                id_to_name: dict[str, str] = {}
                if enrich_names and sf_path.exists():
                    try:
                        facts = self._services.structure_facts_repo.load(
                            config_path=cfg_path,
                            structure_facts_json=sf_path,
                        )
                        id_to_name = self._id_to_name(facts)
                    except Exception:
                        id_to_name = {}

                modules_payload: dict[str, list[dict[str, Any]]] = {}
                for mid in mod_ids:
                    mr = store.list_by_scope(scope_type="module", target_id=mid, limit=tn * 4)
                    mr.sort(key=lambda x: float(x.get("confidence") or 0.0), reverse=True)
                    modules_payload[mid] = mr[:tn]

                st.session_state[cache_key] = {
                    "cfg_resolved": str(cfg_path.resolve()),
                    "system_rows": sys_rows,
                    "modules": modules_payload,
                    "module_ids_all_count": len(all_mod_ids),
                    "id_to_name": id_to_name,
                    "top_n": tn,
                }
                st.success("已从 Weaviate 加载。")
            except Exception as e:
                st.error(f"读取 Weaviate 失败：{e!r}")
            finally:
                if store is not None:
                    try:
                        store.close()
                    except Exception:
                        pass

        cached = st.session_state.get(cache_key)
        if isinstance(cached, dict) and cached.get("cfg_resolved") != str(cfg_path.resolve()):
            st.warning("配置文件路径已变更，请重新点击「从 Weaviate 加载/刷新」。")
            cached = None

        if not isinstance(cached, dict):
            st.info(
                "点击 **从 Weaviate 加载/刷新** 后，将展示系统级（system）与各模块（module）已保存的设计模式/架构模式。"
            )
            return

        sys_rows = cached.get("system_rows") or []
        modules = cached.get("modules") or {}
        id_to_name = cached.get("id_to_name") or {}
        all_cnt = cached.get("module_ids_all_count")

        m1, m2 = st.columns(2)
        with m1:
            st.metric("系统级模式条数", len(sys_rows))
        with m2:
            st.metric("本页已预加载模块数", len(modules))
        if isinstance(all_cnt, int):
            st.caption(f"Weaviate 中合计约有 **{all_cnt}** 个模块含模式数据。")

        if sys_rows:
            st.divider()
            st.markdown("#### 整体架构（scope = system）")
            self._render_pattern_rows(sys_rows, id_to_name=id_to_name, key_base="browse_sys")
        else:
            st.caption("Weaviate 中暂无系统级（system）记录。")

        if modules:
            st.divider()
            mod_keys = sorted(modules.keys())
            pick = st.selectbox("选择模块查看已存模式", options=mod_keys, key=f"{cache_key}_mod_sel")
            st.markdown(f"#### 模块：`{pick}`")
            self._render_pattern_rows(modules.get(pick) or [], id_to_name=id_to_name, key_base=f"browse_mod_{pick}")
        else:
            st.caption("Weaviate 中暂无模块级（module）记录。")
        # 给底部留一点空白，避免最后一个组件（如 st.info/st.caption）在滚动时被视口切割。
        st.markdown("<div style='height:24px;'></div>", unsafe_allow_html=True)

    def _render_run_tab(self) -> None:
        st.markdown("##### 模式识别设置（MVP：仅识别系统/模块级 Top 模式，基于结构证据）")
        st.caption(
            "本页会读取 `structure_facts_for_interpret.json`，提取结构证据后调用 LLM 输出「最可能的设计模式/架构模式」。"
            "结果将持久化到 Weaviate 的 `PatternInterpretation` collection（可断点续跑）。"
        )

        recognize_system = st.checkbox("识别整体架构（system）", value=True)
        st.caption("说明：系统级（system）对整个项目范围做模式识别（不限定模块）。")
        recognize_modules = st.checkbox("识别按模块（scope=module）", value=True)
        st.caption("说明：模块级分别对若干 `module_id` 做模式识别，常用于定位某个子系统的设计/架构风格。")

        col_a, col_b, col_c = st.columns(3)
        with col_a:
            top_n = st.number_input("TopN 模式数", min_value=8, max_value=25, value=12, step=1)
            st.caption("说明：每个 scope（system 或单个 module_id）最多保留并展示/落库的模式条数上限。")
        with col_b:
            max_modules = st.number_input("最多识别模块数", min_value=1, max_value=50, value=8, step=1)
            st.caption(
                "说明：当勾选「识别按模块」时生效。仅取结构事实中代码实体最丰富的前 N 个 `module_id`（按 class/interface/method 数量排序）。"
            )
        with col_c:
            min_confidence = st.slider("最小置信度（过滤低分结果）", min_value=0.0, max_value=1.0, value=0.0, step=0.05)
            st.caption("说明：置信度来自 LLM 输出的 0~1 归一化结果。小于该阈值的模式不会落库/展示（因此最终条数可能少于 TopN）。")

        skip_if_exists = st.checkbox("若已存在结果则跳过（断点续跑）", value=True)
        st.caption("说明：通过查询 Weaviate 判断该 scope 是否已有结果：system 用 (scope_type=system,target_id=system)，module 用 (scope_type=module,target_id=module_id)。若已存在则跳过该 scope 的识别并读取旧结果。")
        clear_collection = st.checkbox("清空该 collection 后重跑（谨慎）", value=False)
        st.caption(
            "说明：清空会删除整个 `PatternInterpretation` collection 的所有记录（含系统级与所有模块）。通常在切换模型/提示词策略后重跑时使用。"
        )

        if not st.button("开始识别", type="primary"):
            return

        cfg_path = self._resolve_cfg_path()
        sf_path = self._resolve_structure_facts_path()

        if not cfg_path.exists():
            st.error(f"配置文件不存在：`{cfg_path}`")
            return
        if not sf_path.exists():
            st.error(f"未找到结构事实文件：`{sf_path}`。请先运行流水线生成缓存。")
            return

        with st.spinner("加载结构事实与配置，准备识别…"):
            pc = self._load_config(str(cfg_path))
            facts = self._services.structure_facts_repo.load(
                config_path=cfg_path,
                structure_facts_json=sf_path,
            )

        llm_cfg = pc.knowledge.business_interpretation
        llm_sel = LLMProviderFactory.from_business_interpretation(llm_cfg)
        llm = llm_sel.provider
        language = llm_cfg.language or "zh"
        llm_timeout = int(llm_cfg.timeout_seconds or 180)

        store, store_err = self._pattern_store_from_vectordb_business(pc)
        if store is None:
            st.warning(store_err or "无法创建 Weaviate 连接。")
            return
        vcfg = pc.knowledge.vectordb_business
        try:
            if clear_collection:
                store.clear()

            module_ids = self._pick_module_ids(facts)[: int(max_modules)]
            if recognize_modules and not module_ids:
                st.warning("未从 structure_facts 中找到可用的 `module_id`，将仅识别系统级（system）。")
                recognize_modules = False

            st.caption(f"LLM 后端：`{llm_sel.resolved_backend}`（超时 {llm_timeout}s）")
            st.caption(
                "模式名称约束：设计模式仅允许 GoF 23 的官方名称；架构模式在常见架构名称集合内选择。"
                "若 LLM 返回的 JSON 无法解析，会触发低置信度的关键词兜底候选。"
            )

            embedding_dim = int(vcfg.dimension or 1024)
            if recognize_system:
                st.write("识别 system（整体架构…）")
                if not (skip_if_exists and store.list_by_scope(scope_type="system", target_id="system", limit=1)):
                    recognize_patterns_for_scope(
                        facts=facts,
                        llm=llm,
                        store=store,
                        embedding_dim=embedding_dim,
                        language=language,
                        scope_type="system",
                        target_id="system",
                        top_n=int(top_n),
                        min_confidence=float(min_confidence),
                        llm_timeout_seconds=llm_timeout,
                    )

            if recognize_modules and module_ids:
                st.write(f"识别模块（共 {len(module_ids)} 个，最多处理 {int(max_modules)} 个…）")
                for i, mid in enumerate(module_ids, start=1):
                    st.caption(f"模块 {i}/{len(module_ids)}：`{mid}`")
                    if skip_if_exists and store.list_by_scope(scope_type="module", target_id=mid, limit=1):
                        continue
                    recognize_patterns_for_scope(
                        facts=facts,
                        llm=llm,
                        store=store,
                        embedding_dim=embedding_dim,
                        language=language,
                        scope_type="module",
                        target_id=mid,
                        top_n=int(top_n),
                        min_confidence=float(min_confidence),
                        llm_timeout_seconds=llm_timeout,
                    )

            st.success("识别完成，正在从 Weaviate 读取结果…")

            if recognize_system:
                sys_rows = store.list_by_scope(scope_type="system", target_id="system", limit=int(top_n * 3))
                sys_rows.sort(key=lambda x: float(x.get("confidence") or 0.0), reverse=True)
                sys_rows = sys_rows[: int(top_n)]
                self._render_pattern_rows(sys_rows, id_to_name=self._id_to_name(facts), key_base="run_sys")

            if recognize_modules and module_ids:
                id_to_name = self._id_to_name(facts)
                mod_select = st.selectbox("选择要展示的模块", options=module_ids)
                mod_rows = store.list_by_scope(scope_type="module", target_id=mod_select, limit=int(top_n * 3))
                mod_rows.sort(key=lambda x: float(x.get("confidence") or 0.0), reverse=True)
                mod_rows = mod_rows[: int(top_n)]
                st.divider()
                st.markdown(f"#### 模块：`{mod_select}`")
                self._render_pattern_rows(mod_rows, id_to_name=id_to_name, key_base=f"run_mod_{mod_select}")
        finally:
            try:
                store.close()
            except Exception:
                pass
        # 同样给底部留一点空白，避免滚动时底部提示区被切割。
        st.markdown("<div style='height:24px;'></div>", unsafe_allow_html=True)

    @staticmethod
    def _id_to_name(facts) -> dict[str, str]:
        m: dict[str, str] = {}
        for e in facts.entities:
            if e.id and (e.name or ""):
                m[e.id] = e.name
        return m

    def _render_pattern_rows(
        self,
        rows: list[dict[str, Any]],
        *,
        id_to_name: dict[str, str],
        key_base: str = "pattern",
    ) -> None:
        if not rows:
            st.info("本 scope 下暂无可展示的模式结果。")
            return

        def _truncate_text(s: str, n: int) -> str:
            s = (s or "").strip()
            if not s:
                return ""
            if len(s) <= n:
                return s
            return s[:n] + "…"

        def _ptype_label(pt: str) -> str:
            pt = (pt or "").strip().lower()
            if pt == "design":
                return "设计模式"
            if pt == "architecture":
                return "架构模式"
            return pt or "模式"

        key_base = (key_base or "pattern").strip()

        design_rows: list[dict[str, Any]] = []
        arch_rows: list[dict[str, Any]] = []
        other_rows: list[dict[str, Any]] = []
        for r in rows:
            pt = (r.get("pattern_type") or "").strip().lower()
            if pt == "design":
                design_rows.append(r)
            elif pt == "architecture":
                arch_rows.append(r)
            else:
                other_rows.append(r)

        def _render_one(idx: int, r: dict[str, Any]) -> None:
            ptype = r.get("pattern_type") or ""
            pname = r.get("pattern_name") or ""
            conf = r.get("confidence") or 0.0
            summary = r.get("summary_text") or ""
            evidence_json = r.get("evidence_json") or "{}"

            evidence: dict[str, Any] = {}
            try:
                evidence = json.loads(evidence_json) if evidence_json else {}
            except Exception:
                evidence = {}

            notes = evidence.get("notes") or evidence.get("reason") or ""
            description = evidence.get("description") or evidence.get("detail_description") or ""

            entity_ids = evidence.get("entity_ids") or evidence.get("entities") or []
            if isinstance(entity_ids, str):
                entity_ids = [entity_ids]
            if not isinstance(entity_ids, list):
                entity_ids = []
            entity_ids = [str(x) for x in entity_ids if x]

            p_label = _ptype_label(ptype)
            try:
                conf_f = float(conf)
                conf_text = f"{conf_f:.2f}"
            except Exception:
                conf_text = str(conf) or "0"

            exp_key = hashlib.md5(
                f"{key_base}|{p_label}|{pname}|{idx}".encode("utf-8")
            ).hexdigest()[:12]

            # 每个 pattern 一个独立“带边框容器”：用 expander 承载详情内容，并可通过折叠关闭。
            # 部分 Streamlit 版本的 st.expander 不支持 key= 参数；这里不显式传 key，
            # 内部 toggle 的 key 使用 exp_key 保证唯一性。
            with st.expander(
                f"{p_label}：{_truncate_text(str(pname), 80)}",
                expanded=False,
            ):
                st.caption(f"置信度：{conf_text}")

                if summary:
                    st.caption(_truncate_text(str(summary), 260))

                if description:
                    st.caption(f"描述：{_truncate_text(str(description), 260)}")

                if notes:
                    st.caption(f"证据推断：{_truncate_text(str(notes), 260)}")

                if entity_ids:
                    preview_ids = entity_ids[:8]
                    entity_lines: list[str] = []
                    for eid in preview_ids:
                        display = id_to_name.get(eid) or ""
                        display = _truncate_text(str(display), 40) if display else ""
                        entity_lines.append(f"- {eid}：{display}" if display else f"- {eid}")

                    st.markdown("**相关实体预览（最多 8 个）**")
                    st.markdown("\n".join(entity_lines))
                    if len(entity_ids) > 8:
                        st.caption(f"...还有 {len(entity_ids) - 8} 个实体")

                    show_more_entities = st.toggle(
                        "展开更多相关实体（最多 20 个）",
                        value=False,
                        key=f"{exp_key}_more_entities",
                    )
                    if show_more_entities:
                        full_preview = entity_ids[:20]
                        full_lines: list[str] = []
                        for eid in full_preview:
                            display = id_to_name.get(eid) or ""
                            display = _truncate_text(str(display), 60) if display else ""
                            full_lines.append(f"- {eid}：{display}" if display else f"- {eid}")
                        st.markdown("**相关实体（最多 20 个）**")
                        st.markdown("\n".join(full_lines))

                show_raw = st.toggle(
                    "查看 evidence_json 原文",
                    value=False,
                    key=f"{exp_key}_raw",
                )
                if show_raw:
                    st.code(str(evidence_json)[:20000], language="json")

        # 让分类更清晰：在容器列表上方显示“设计/架构”分组标题
        if design_rows:
            st.markdown("#### 设计模式")
            for i, rr in enumerate(design_rows):
                _render_one(i, rr)
        if arch_rows:
            st.markdown("#### 架构模式")
            for i, rr in enumerate(arch_rows):
                _render_one(i, rr)
        if other_rows:
            st.markdown("#### 其他模式")
            for i, rr in enumerate(other_rows):
                _render_one(i, rr)

