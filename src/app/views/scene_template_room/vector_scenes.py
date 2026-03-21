from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

import streamlit as st

from src.app.components.interpretation_panel import InterpretationPanel
from src.app.views.scene_template_room.scene_context import SceneTemplateContext
from src.core.weaviate_defaults import DEFAULT_WEAVIATE_HTTP_URL
from src.knowledge.business_question_lexical_rerank import (
    RERANK_IDENT_LEXICAL_WEIGHT,
    RERANK_LEXICAL_WEIGHT,
    RERANK_POOL_CAP,
    RERANK_RECALL_MULT,
    merge_method_hits_max_score,
    rerank_hits_by_lexical_overlap,
)
from src.knowledge.method_entity_id_normalize import normalize_method_entity_id
from src.semantic.embedding import get_embedding


def _one_line_ellipsis(s: str, max_len: int = 128) -> str:
    t = " ".join(((s or "").strip()).split())
    if len(t) <= max_len:
        return t
    return t[: max_len - 1] + "…"


@dataclass
class BusinessQuestionToCodePM:
    """业务问题->代码场景的展示模型：承载输入、检索结果与调试信息，render 仅负责控件。"""
    question: str
    top_k: int
    run: bool
    use_cached_hits: bool
    validation_error: str | None = None
    hits: list[tuple[str, float]] = field(default_factory=list)
    debug_info: dict[str, Any] = field(default_factory=dict)
    selected_src_eid: str | None = None

    @property
    def should_early_exit(self) -> bool:
        """未点击运行且无缓存：只展示表单，不展示 TopN。"""
        return (not self.run) and (not self.use_cached_hits)

    @property
    def should_show_results(self) -> bool:
        """已进入检索流程、无校验错误且 hits 非空（空列表由界面另行展示提示）。"""
        return not self.should_early_exit and self.validation_error is None and bool(self.hits)


def build_business_question_to_code_pm(
    ctx: SceneTemplateContext,
    question: str,
    top_k: int,
    run: bool,
    *,
    cache: dict[str, Any],
    scene_key: str = "scene_business_question_to_code",
) -> BusinessQuestionToCodePM:
    """
    根据上下文与表单输入构建展示模型；不调用 Streamlit 控件 API。

    会写入 ``cache``（通常为 ``st.session_state``）中的命中列表与查询参数键；
    在需刷新结果时调用 Weaviate 检索、词面重排及（可选）嵌入维度探测，非数学意义上的纯函数。
    """
    # 键名含固定后缀：勿改，否则已打开页面的会话缓存会失效（用户需重新点「运行」）
    hits_cache_key = f"{scene_key}_hits_cache_v7"
    q_cache_key = f"{scene_key}_question_cache"
    topk_cache_key = f"{scene_key}_topk_cache"
    selected_key = f"{scene_key}_selected_src_eid"

    use_cached = (
        (not run)
        and cache.get(q_cache_key) == question
        and cache.get(topk_cache_key) == int(top_k)
        and isinstance(cache.get(hits_cache_key), list)
    )
    selected_src_eid: str | None = cache.get(selected_key)

    if ctx.method_interpret_store is None and ctx.business_interpret_store is None:
        return BusinessQuestionToCodePM(
            question=question,
            top_k=int(top_k),
            run=bool(run),
            use_cached_hits=False,
            validation_error=(
                "未配置或未启用 Weaviate「方法技术解读 / 业务解读」向量库（knowledge.vectordb-interpret 与 "
                "knowledge.vectordb-business）。无法进行本场景的语义检索。"
            ),
            selected_src_eid=selected_src_eid,
        )

    if use_cached:
        hits = cache.get(hits_cache_key) or []
        vectordb_caption = ""
        try:
            vi = ctx.config_view.knowledge.vectordb_interpret
            vb = ctx.config_view.knowledge.vectordb_business
            icol = vi.collection_name or "MethodInterpretation"
            bcol = vb.collection_name or "BusinessInterpretation"
            iurl = vi.weaviate_url or vb.weaviate_url or DEFAULT_WEAVIATE_HTTP_URL
            idim = int(vi.dimension or 1024)
            bdim = int(vb.dimension or 1024)
            tech_cnt = ctx.method_interpret_store.count() if ctx.method_interpret_store else 0
            biz_cnt = ctx.business_interpret_store.count() if ctx.business_interpret_store else 0
            vectordb_caption = (
                f"Debug：检索源=技术解读+业务解读 | tech={icol} count≈{tech_cnt} dim={idim} | "
                f"biz={bcol} count≈{biz_cnt} dim={bdim} | url={iurl}"
            )
        except Exception:
            # 配置视图或 count 失败：仍返回缓存 hits，仅降级本条 debug 文案
            vectordb_caption = "Debug：无法获取解读库状态（忽略）。"
        return BusinessQuestionToCodePM(
            question=question,
            top_k=int(top_k),
            run=bool(run),
            use_cached_hits=True,
            hits=hits,
            debug_info={
                "cached_hits_count": len(hits),
                "top_k": int(top_k),
                "vectordb_caption": vectordb_caption,
                "status_caption": (
                    f"Debug：使用缓存命中数量={len(hits)}（TopK={int(top_k)}；"
                    "词面重排：问句词 + project 域词表，过滤 YAML 泛英文噪声；业务解读库侧近邻后筛 API 级实体）"
                ),
            },
            selected_src_eid=selected_src_eid,
        )

    pool = min(
        max(int(top_k) * RERANK_RECALL_MULT, int(top_k)),
        RERANK_POOL_CAP,
    )
    tech_hits = (
        ctx.method_interpret_store.search_by_text(question, top_k=pool)
        if ctx.method_interpret_store
        else []
    )
    biz_hits = (
        ctx.business_interpret_store.search_method_hits_by_text(question, top_k=pool)
        if ctx.business_interpret_store
        else []
    )
    merged = merge_method_hits_max_score(tech_hits, biz_hits, pool=pool)
    hits, avg_lex, lex_terms, lex_base_n, lex_vocab_n = rerank_hits_by_lexical_overlap(
        question,
        ctx.config_view.domain,
        merged,
        final_top_k=int(top_k),
        get_node=ctx.get_node,
        method_interpret_store=ctx.method_interpret_store,
        business_interpret_store=ctx.business_interpret_store,
    )

    try:
        vi = ctx.config_view.knowledge.vectordb_interpret
        vb = ctx.config_view.knowledge.vectordb_business
        icol = vi.collection_name or "MethodInterpretation"
        bcol = vb.collection_name or "BusinessInterpretation"
        iurl = vi.weaviate_url or vb.weaviate_url or DEFAULT_WEAVIATE_HTTP_URL
        idim = int(vi.dimension or 1024)
        bdim = int(vb.dimension or 1024)
        tech_cnt = ctx.method_interpret_store.count() if ctx.method_interpret_store else 0
        biz_cnt = ctx.business_interpret_store.count() if ctx.business_interpret_store else 0
        vectordb_caption = (
            f"Debug：检索源=技术解读+业务解读 | tech={icol} count≈{tech_cnt} dim={idim} | "
            f"biz={bcol} count≈{biz_cnt} dim={bdim} | url={iurl}"
        )
        edim = max(idim, bdim)
        query_emb_caption = ""
        try:
            vec = get_embedding(question, edim)
            nonzero = sum(1 for x in vec if abs(float(x)) > 1e-12)
            query_emb_caption = f"query_embedding_len={len(vec)}（max_dim={edim}）| nonzero_cnt={nonzero}/{len(vec)}"
        except Exception:
            # 仅影响 debug 行展示，检索已在各 store 内完成
            pass
    except Exception:
        # 解读库配置不可读：检索结果仍写入下方 cache，仅省略 vectordb / embedding 的 debug
        vectordb_caption = "Debug：无法获取解读库状态（忽略）。"
        query_emb_caption = ""

    cache[hits_cache_key] = hits
    cache[q_cache_key] = question
    cache[topk_cache_key] = int(top_k)

    di: dict[str, Any] = {
        "pool": pool,
        "merged_n": len(merged),
        "tech_n": len(tech_hits),
        "biz_n": len(biz_hits),
        "lex_terms": lex_terms,
        "lex_base_n": lex_base_n,
        "lex_vocab_n": lex_vocab_n,
        "avg_lex": avg_lex,
        "vectordb_caption": vectordb_caption,
        "query_embedding_caption": query_emb_caption,
    }
    di["status_caption"] = (
        f"Debug：扩召回 pool={pool}，合并去重后候选={len(merged)}；"
        f"技术解读 {len(tech_hits)} + 业务解读 {len(biz_hits)}；"
        f"词面词项：问句基础 {lex_base_n}，合并 project 域/能力/术语后共 {len(lex_terms)} "
        f"（配置词库≈{lex_vocab_n}，仅 name 命中时追加域 id / 能力 id / 域下 capability_ids）；"
        f"候选平均词面命中率≈{avg_lex:.2f}；"
        f"重排后展示 TopK={len(hits)}（score≈向量分+{RERANK_LEXICAL_WEIGHT}×全文词面命中率"
        f"+{RERANK_IDENT_LEXICAL_WEIGHT}×方法名/签名命中率）"
    )
    return BusinessQuestionToCodePM(
        question=question,
        top_k=int(top_k),
        run=bool(run),
        use_cached_hits=False,
        hits=hits,
        selected_src_eid=selected_src_eid,
        debug_info=di,
    )


def _render_business_question_to_code_results(
    pm: BusinessQuestionToCodePM,
    ctx: SceneTemplateContext,
    scene_key: str = "scene_business_question_to_code",
) -> None:
    """仅负责 TopN 列表与源码展开的渲染；PM 已承载全部数据。"""
    selected_key = f"{scene_key}_selected_src_eid"
    backend = ctx.get_graph_backend_memory_first()

    st.subheader("向量检索 TopN")
    st.caption(
        f"score 列：向量相似分 + {RERANK_LEXICAL_WEIGHT} ×（词项在全文解读/方法名等中的命中比例）"
        f"+ {RERANK_IDENT_LEXICAL_WEIGHT} ×（词项在方法名与签名中的命中比例；"
        f"camelCase 会拆成子词；英文词项按整词匹配，避免短词子串误命中）。"
        f"词项 = 问句抽取 + `config/project.yaml` 的 `domain.business_domains` / `domain.capabilities` / "
        f"`domain.terms`（含同义词）中与问句相关的短语；"
        f"业务域/能力的 **id**（及域所挂 `capability_ids`）仅在 **对应 name** 与问句相关时加入，避免无关 id 噪声。"
        f"用于与解读/方法名等文本做子串匹配。"
    )
    if not pm.hits:
        st.info(
            "未检索到候选方法。请确认已在流水线中生成「方法技术解读」与/或「业务解读（API 级）」并写入 Weaviate。"
        )
        return

    selected_src_eid: str | None = pm.selected_src_eid
    for idx, (eid, score) in enumerate(pm.hits[: int(pm.top_k)], start=1):
        node = ctx.get_node(eid) if backend is not None else None
        disp = ctx.method_listing_display(eid)
        title = disp["title"]
        sig_d = (disp.get("signature") or "").strip()
        cls_d = (disp.get("class_name") or "").strip()

        key_hash = hashlib.md5(f"{scene_key}|{eid}|{idx}".encode("utf-8")).hexdigest()[:12]

        with st.container(border=True):
            cols = st.columns([6, 2, 2])
            with cols[0]:
                st.markdown(f"**{idx}. {title}**")
                meta_bits: list[str] = []
                if sig_d:
                    meta_bits.append(_one_line_ellipsis(sig_d, 140))
                if cls_d:
                    meta_bits.append(f"类：`{_one_line_ellipsis(cls_d, 60)}`")
                if meta_bits:
                    st.caption(" · ".join(meta_bits))
                st.caption(f"`{eid}`")
            with cols[1]:
                st.caption("score")
                st.write(f"{float(score):.4f}")
            with cols[2]:
                if st.button("查看源码", key=f"{scene_key}_view_src_{key_hash}", type="secondary"):
                    selected_src_eid = None if selected_src_eid == eid else eid
                    st.session_state[selected_key] = selected_src_eid

            if selected_src_eid == eid:
                InterpretationPanel.render(
                    eid,
                    "method",
                    node,
                    ctx.weaviate_data_svc,
                    wrap_in_expander=False,
                )


class BusinessQuestionToCodeScene:
    key = "scene_business_question_to_code"
    title = "业务问题 -> 找对应代码逻辑（向量语义检索 + 图谱扩展）"

    def render(self, ctx: SceneTemplateContext) -> None:
        with st.container(border=True):
            st.markdown(f"### {self.title}")
            st.caption(
                "自然语言问题 -> 在 Weaviate「方法技术解读 + 业务解读（API 级）」中做向量检索；"
                "先扩召回（池子较大以便把「方法名相关」的候选捞进来），再按问句词项 + project.yaml 域/能力/术语扩展"
                "（域/能力 id 仅 name 命中时加入），对全文解读做词面分，并对方法名/签名单独加成"
                "（camelCase 拆词，如 deleteOrder ↔ order）。不重建向量数据。"
                "源码仍从 CodeEntity 拉取（点「查看源码」）。"
            )

            with st.form("form_business_question_to_code", clear_on_submit=False):
                question = st.text_area(
                    "业务问题（示例：用户注册后做了哪些鉴权与落库流程？）",
                    value="用户注册后，系统如何完成鉴权并将用户信息落库？",
                    height=90,
                )
                top_k = st.slider("TopK 候选方法", min_value=3, max_value=30, value=10, step=1)
                run = st.form_submit_button("运行", type="primary")

            pm = build_business_question_to_code_pm(
                ctx, question, int(top_k or 10), bool(run),
                cache=st.session_state,
                scene_key=self.key,
            )
            if pm.should_early_exit:
                return
            if pm.validation_error:
                st.warning(pm.validation_error)
                return

            di = pm.debug_info
            if di.get("vectordb_caption"):
                st.caption(di["vectordb_caption"])
            if di.get("query_embedding_caption"):
                st.caption(f"Debug：{di['query_embedding_caption']}")
            if di.get("status_caption"):
                st.caption(di["status_caption"])

            _render_business_question_to_code_results(pm, ctx, scene_key=self.key)


def _fetch_tech_interpret_text(ctx: SceneTemplateContext, method_id: str) -> str:
    """MethodInterpretation：拼接解读正文与摘要。"""
    if ctx.method_interpret_store is None:
        return ""
    try:
        inter = ctx.method_interpret_store.get_by_method_id(method_id)
        if not inter:
            return ""
        parts: list[str] = []
        t1 = str(inter.get("interpretation_text") or "").strip()
        t2 = str(inter.get("context_summary") or "").strip()
        if t1:
            parts.append(t1)
        if t2:
            parts.append(t2)
        return "\n\n".join(parts)
    except Exception:
        return ""


def _fetch_biz_interpret_text(ctx: SceneTemplateContext, method_id: str) -> str:
    """BusinessInterpretation level=api：业务综述。"""
    if ctx.business_interpret_store is None:
        return ""
    try:
        biz = ctx.business_interpret_store.get_by_entity(method_id, level="api")
        if not biz:
            return ""
        return str(biz.get("summary_text") or "").strip()
    except Exception:
        return ""


class ReverseCodeToIntentScene:
    key = "scene_reverse_code_to_intent"
    title = "反向定位：从代码片段找业务意图（向量 -> 图谱）"

    def render(self, ctx: SceneTemplateContext) -> None:
        with st.container(border=True):
            st.markdown(f"### {self.title}")
            st.caption(
                "粘贴代码片段或方法签名，与 **代码向量库** 做相似度检索，**TopK 默认 3、滑条最多 5**；"
                "若该方法已有技术解读 / 业务解读，则在下方直接展示。"
            )

            with st.form("form_reverse_code_to_intent", clear_on_submit=False):
                code_like = st.text_area(
                    "代码片段或签名（可含注释）",
                    value="public void loginCheck(User user){ /* 校验token并写入鉴权记录 */ }",
                    height=140,
                )
                top_k = st.slider("候选 TopK", min_value=1, max_value=5, value=3, step=1)
                run = st.form_submit_button("运行", type="primary")

            if not run:
                return

            if ctx.code_vector_store is None:
                st.warning("未启用 code 向量库（knowledge.vectordb-code 等），无法进行代码相似度检索。")
                return

            tk = int(top_k)
            hits = ctx.code_vector_store.search_by_text(code_like, top_k=tk)
            if not hits:
                st.info("未检索到相似代码实体，请尝试更长片段或不同签名。")
                return

            st.subheader(f"Top {tk} 相似结果（代码向量）")

            for rank, (eid, score) in enumerate(hits[:tk], start=1):
                disp = ctx.method_listing_display(eid)
                title = disp.get("title") or eid
                sig_d = (disp.get("signature") or "").strip()
                cls_d = (disp.get("class_name") or "").strip()

                is_method = eid.startswith("method://") or eid.startswith("method//")
                mid = normalize_method_entity_id(eid) if is_method else eid

                with st.container(border=True):
                    st.markdown(f"**{rank}. {title}** · `score={float(score):.4f}`")
                    meta_bits: list[str] = []
                    if sig_d:
                        meta_bits.append(_one_line_ellipsis(sig_d, 160))
                    if cls_d:
                        meta_bits.append(f"类：`{_one_line_ellipsis(cls_d, 80)}`")
                    if meta_bits:
                        st.caption(" · ".join(meta_bits))
                    st.caption(f"`{eid}`")

                    if not is_method:
                        st.info(
                            "当前命中实体非方法（如 class），技术解读 / 业务解读仅在 **方法 API 级** 生成；"
                            "可换更短的方法体或单独方法签名重试。"
                        )
                        continue

                    tech = _fetch_tech_interpret_text(ctx, mid)
                    biz = _fetch_biz_interpret_text(ctx, mid)

                    st.markdown("**技术解读：**")
                    if tech:
                        st.markdown(tech)
                    else:
                        st.caption("（暂无技术解读）")

                    st.markdown("**业务解读：**")
                    if biz:
                        st.markdown(biz)
                    else:
                        st.caption("（暂无业务解读）")
