"""
业务问句 → 方法候选的词面重排（无 Streamlit、无全局可变状态）。

在向量 near_vector 候选上叠加问句词项与 project.yaml 域词汇的命中比例，
不重建向量；供「业务问题 -> 找代码」等场景使用。解读库读取失败时跳过对应片段，仍以节点字段参与匹配。
"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional, Protocol

from src.knowledge.method_entity_id_normalize import normalize_method_entity_id


class _MethodInterpretStoreLike(Protocol):
    def get_by_method_id(self, method_id: str) -> dict[str, Any] | None: ...


class _BusinessInterpretStoreLike(Protocol):
    def get_by_entity(self, entity_id: str, level: str) -> dict[str, Any] | None: ...


# 向量分 + 词面混合分（问句词在全文解读中的命中比例）
RERANK_LEXICAL_WEIGHT = 0.42
# 方法名/签名中命中问句词项的额外加成
RERANK_IDENT_LEXICAL_WEIGHT = 0.28
# 扩召回倍数：每路 near_vector 多取，再合并重排截断到 TopK
RERANK_RECALL_MULT = 9
RERANK_POOL_CAP = 160

_RERANK_PART_SPLIT = re.compile(
    r"[的了吗呢啊吧和与在为对从中到及、，。；：？！\?\!\s\n\r\t]+"
)
_RERANK_SKIP_TERMS = frozenset(
    {"什么", "如何", "哪些", "是否", "怎么", "怎样", "请问", "这个", "那个", "一个"}
)
_RERANK_CJK_GENERIC_BIGRAMS = frozenset(
    {
        "管理",
        "流程",
        "场景",
        "处理",
        "数据",
        "信息",
        "系统",
        "服务",
        "模块",
        "相关",
        "功能",
        "接口",
        "业务",
        "代码",
        "方法",
        "实现",
        "逻辑",
        "应用",
        "配置",
        "参数",
        "结果",
        "内容",
        "对象",
        "实体",
        "组件",
        "页面",
        "请求",
        "响应",
        "用户",
        "操作",
    }
)
_RERANK_ASCII_GENERIC_TOKENS = frozenset(
    {
        "get",
        "set",
        "put",
        "add",
        "del",
        "new",
        "old",
        "all",
        "any",
        "key",
        "map",
        "list",
        "item",
        "name",
        "type",
        "data",
        "info",
        "code",
        "api",
        "url",
        "uri",
        "id",
        "obj",
        "ref",
        "update",
        "create",
        "delete",
        "remove",
        "query",
        "select",
        "fetch",
        "load",
        "save",
        "find",
        "build",
        "copy",
        "clear",
        "open",
        "close",
        "init",
        "read",
        "write",
        "send",
        "recv",
        "call",
        "exec",
        "run",
        "handle",
        "process",
        "check",
        "valid",
        "parse",
        "format",
        "convert",
    }
)
_RERANK_ASCII_DOMAIN_KEEP = frozenset(
    {
        "order",
        "orders",
        "cart",
        "payment",
        "pay",
        "refund",
        "invoice",
        "product",
        "products",
        "sku",
        "inventory",
        "shipment",
        "coupon",
        "user",
        "users",
        "auth",
        "login",
        "session",
        "token",
        "tenant",
        "stock",
        "price",
        "amount",
    }
)


def merge_method_hits_max_score(
    tech: list[tuple[str, float]],
    biz: list[tuple[str, float]],
    *,
    pool: int,
) -> list[tuple[str, float]]:
    """两路命中按 method_id 合并，同一方法取最大向量分，再全局排序取前 pool 条（用于后续词面重排）。"""
    scores: dict[str, float] = {}
    for mid, s in tech + biz:
        m = normalize_method_entity_id(str(mid or "").strip())
        if not m:
            continue
        scores[m] = max(scores.get(m, 0.0), float(s))
    ranked = sorted(scores.keys(), key=lambda k: scores[k], reverse=True)
    return [(k, scores[k]) for k in ranked[: int(pool)]]


def soft_token_flat_for_ascii_match(text: str) -> str:
    """
    将方法名/解读文本中的英文标识软分词并小写，供 ASCII 词项做「整词」匹配，
    避免 `or` 命中 `order`、`api` 命中 `capital` 等子串污染。
    """
    if not text or not isinstance(text, str):
        return ""
    x = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    x = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", x)
    x = x.lower()
    x = re.sub(r"[_\-.]+", " ", x)
    x = re.sub(r"[^\w\u4e00-\u9fff]+", " ", x, flags=re.UNICODE)
    return re.sub(r"\s+", " ", x).strip()


def ascii_lexical_hit(term: str, flat: str) -> bool:
    """flat 为 soft_token_flat_for_ascii_match 的输出（小写、空白分隔）。"""
    tl = (term or "").strip().lower()
    if len(tl) < 2 or not flat:
        return False
    return bool(
        re.search(
            r"(?<![a-z0-9])" + re.escape(tl) + r"(?![a-z0-9])",
            flat,
        )
    )


def split_identifier_for_lexical_match(ident: str) -> str:
    """
    将 camelCase / snake_case 方法名拆成可读片段，拼入词面匹配文本，
    使问句中的 order 能命中 deleteOrder、order_global 等标识符。
    """
    if not ident or not isinstance(ident, str):
        return ""
    s = ident.strip()
    if not s:
        return ""
    s = re.sub(r"[_\-\s]+", " ", s)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
    toks = [x.lower() for x in s.split() if len(x) >= 2]
    return " ".join(toks)


def _cjk_prefix_suffix_from_phrase(phrase: str) -> list[str]:
    """长中文短语在解读里常只出现核心二字词；从问句基础词补前缀/后缀二字，提高命中。"""
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", phrase or ""))
    if len(cjk) < 4:
        return []
    a, b = cjk[:2], cjk[-2:]
    out: list[str] = []
    if len(a) == 2 and a not in _RERANK_CJK_GENERIC_BIGRAMS:
        out.append(a)
    if len(b) == 2 and b != a and b not in _RERANK_CJK_GENERIC_BIGRAMS:
        out.append(b)
    return out


def _should_drop_ascii_synonym_noise(term: str, question: str) -> bool:
    """去掉 YAML 带入的泛英文词（问句里没出现），减轻 get/update/create 类方法词面虚高。"""
    if not term.isascii():
        return False
    tl = term.strip().lower()
    if tl not in _RERANK_ASCII_GENERIC_TOKENS:
        return False
    if tl in _RERANK_ASCII_DOMAIN_KEEP:
        return False
    flat_q = soft_token_flat_for_ascii_match(question)
    if ascii_lexical_hit(term, flat_q):
        return False
    return True


def query_terms_for_rerank(question: str) -> list[str]:
    """从自然语言问题抽取短词串/英文词，用于子串命中（无需分词库）。"""
    q = (question or "").strip()
    if not q:
        return []
    terms: list[str] = []
    seen: set[str] = set()

    def add(t: str) -> None:
        t = t.strip()
        if len(t) < 2 or t in _RERANK_SKIP_TERMS:
            return
        if t not in seen:
            seen.add(t)
            terms.append(t)

    for m in re.finditer(r"[A-Za-z][A-Za-z0-9_]{2,}", q):
        add(m.group(0).lower())

    for part in _RERANK_PART_SPLIT.split(q):
        cjk = "".join(re.findall(r"[\u4e00-\u9fff]", part))
        if len(cjk) < 2:
            continue
        if len(cjk) <= 12:
            add(cjk)
        else:
            for ln in (2, 3):
                for i in range(0, len(cjk) - ln + 1):
                    add(cjk[i : i + ln])
    return terms[:60]


def collect_domain_lexical_vocab(domain_cfg: dict[str, Any] | None) -> frozenset[str]:
    """
    从 project.yaml 的 domain 段收集名称与术语短语（不含 id），供子串扩展。
    """
    out: set[str] = set()
    if not isinstance(domain_cfg, dict):
        return frozenset()
    dom = domain_cfg

    for bd in dom.get("business_domains") or []:
        if not isinstance(bd, dict):
            continue
        v = bd.get("name")
        if isinstance(v, str) and len(v.strip()) >= 2:
            out.add(v.strip())

    for cap in dom.get("capabilities") or []:
        if not isinstance(cap, dict):
            continue
        v = cap.get("name")
        if isinstance(v, str) and len(v.strip()) >= 2:
            out.add(v.strip())

    for term in dom.get("terms") or []:
        if not isinstance(term, dict):
            continue
        v = term.get("name")
        if isinstance(v, str) and len(v.strip()) >= 2:
            out.add(v.strip())
        for s in term.get("synonyms") or []:
            if isinstance(s, str):
                s2 = s.strip()
                if len(s2) >= 2:
                    out.add(s2)
                elif len(s2) == 1 and s2.isalnum():
                    out.add(s2)

    return frozenset(out)


def expand_terms_with_project_domain_vocab(
    base_terms: list[str],
    question: str,
    vocab: frozenset[str],
    domain_cfg: dict[str, Any] | None,
    *,
    max_terms: int = 100,
) -> list[str]:
    """
    在问句词基础上，合并 project.yaml 中与问句相关的域/能力/术语短语。
    """
    seen: set[str] = set()
    out: list[str] = []
    q = (question or "").strip()

    def _name_touched(nm: str) -> bool:
        if not isinstance(nm, str) or len(nm.strip()) < 2:
            return False
        nm = nm.strip()
        if nm in q:
            return True
        return any(len(t) >= 2 and (nm in t or t in nm) for t in base_terms)

    def _append(s: str) -> bool:
        s = (s or "").strip()
        if len(s) < 1:
            return False
        if len(s) == 1 and not s.isalnum():
            return False
        if s in seen:
            return False
        seen.add(s)
        out.append(s)
        return len(out) >= max_terms

    for t in base_terms:
        if _append(t):
            return out[:max_terms]

    for p in vocab:
        if len(p) < 2:
            continue
        if p in q:
            if _append(p):
                return out[:max_terms]

    for t in base_terms:
        if len(t) < 2:
            continue
        for p in vocab:
            if len(p) < 2 or p in seen:
                continue
            if t in p or (len(p) <= len(t) and p in t):
                if _append(p):
                    return out[:max_terms]

    if isinstance(domain_cfg, dict):
        for term in domain_cfg.get("terms") or []:
            if not isinstance(term, dict):
                continue
            nm = term.get("name")
            if not isinstance(nm, str) or len(nm.strip()) < 2:
                continue
            nm = nm.strip()
            touched = nm in q or any(
                (len(t) >= 2 and (nm in t or t in nm)) for t in base_terms
            )
            if not touched:
                continue
            for s in term.get("synonyms") or []:
                if not isinstance(s, str):
                    continue
                s2 = s.strip()
                if len(s2) < 1:
                    continue
                if len(s2) == 1 and s2.isascii() and s2.isalnum():
                    continue
                if s2.isascii():
                    s2 = s2.lower()
                if _append(s2):
                    return out[:max_terms]

        for bd in domain_cfg.get("business_domains") or []:
            if not isinstance(bd, dict):
                continue
            bname = bd.get("name")
            if not isinstance(bname, str) or not _name_touched(bname):
                continue
            bid = bd.get("id")
            if isinstance(bid, str) and len(bid.strip()) >= 2:
                if _append(bid.strip()):
                    return out[:max_terms]
            for cid in bd.get("capability_ids") or []:
                if isinstance(cid, str) and len(cid.strip()) >= 2:
                    if _append(cid.strip()):
                        return out[:max_terms]

        for cap in domain_cfg.get("capabilities") or []:
            if not isinstance(cap, dict):
                continue
            cname = cap.get("name")
            if not isinstance(cname, str) or not _name_touched(cname):
                continue
            cid = cap.get("id")
            if isinstance(cid, str) and len(cid.strip()) >= 2:
                if _append(cid.strip()):
                    return out[:max_terms]

    if not out and q:
        for p in vocab:
            if len(p) >= 2 and p in q:
                if _append(p):
                    return out[:max_terms]
    return out[:max_terms]


def build_lexical_terms_for_rerank(
    question: str, domain_cfg: dict[str, Any] | None
) -> tuple[list[str], int, int]:
    """(合并后词表, 基础词项数, project 域/能力/术语词表规模)。"""
    base = query_terms_for_rerank(question)
    vocab = collect_domain_lexical_vocab(domain_cfg)
    dom = domain_cfg if isinstance(domain_cfg, dict) else None
    merged = expand_terms_with_project_domain_vocab(base, question, vocab, dom)
    seen_m = set(merged)
    for bt in base:
        for frag in _cjk_prefix_suffix_from_phrase(bt):
            if frag not in seen_m and len(frag) == 2:
                seen_m.add(frag)
                merged.append(frag)
    merged = [t for t in merged if not _should_drop_ascii_synonym_noise(t, question)]
    return merged, len(base), len(vocab)


def method_text_blob_for_rerank(
    method_id: str,
    node: dict[str, Any] | None,
    *,
    method_interpret_store: Optional[_MethodInterpretStoreLike],
    business_interpret_store: Optional[_BusinessInterpretStoreLike],
) -> str:
    """拼接方法名、签名、技术解读、业务解读（API）供词面匹配。"""
    parts: list[str] = []
    nd = node or {}
    nm = str(nd.get("name") or "")
    sig = str(nd.get("signature") or "")
    parts.append(nm)
    parts.append(sig)
    id_extra = split_identifier_for_lexical_match(nm)
    if id_extra:
        parts.append(id_extra)
    sig_id = split_identifier_for_lexical_match(sig)
    if sig_id:
        parts.append(sig_id)
    try:
        if method_interpret_store is not None:
            inter = method_interpret_store.get_by_method_id(method_id)
            if inter:
                parts.append(str(inter.get("interpretation_text") or ""))
                parts.append(str(inter.get("context_summary") or ""))
    except Exception:
        # 单条方法的技术解读拉取失败时忽略，仍用语义节点上的 name/signature 等
        pass
    try:
        if business_interpret_store is not None:
            biz = business_interpret_store.get_by_entity(method_id, level="api")
            if biz:
                parts.append(str(biz.get("summary_text") or ""))
                parts.append(str(biz.get("business_domain") or ""))
                parts.append(str(biz.get("business_capabilities") or ""))
    except Exception:
        # 单条方法的 API 级业务解读拉取失败时忽略
        pass
    return "\n".join(parts)


def rerank_hits_by_lexical_overlap(
    question: str,
    domain_cfg: dict[str, Any] | None,
    hits: list[tuple[str, float]],
    *,
    final_top_k: int,
    get_node: Callable[[str], dict[str, Any] | None],
    method_interpret_store: Optional[_MethodInterpretStoreLike] = None,
    business_interpret_store: Optional[_BusinessInterpretStoreLike] = None,
) -> tuple[list[tuple[str, float]], float, list[str], int, int]:
    """
    在向量候选上叠加词面分：问句词项（含 project.yaml 扩展）在全文解读中的命中比例，
    以及同一批词项在方法名/签名（含 camelCase 拆词）中的命中比例。
    返回 (重排列表, 平均全文词面命中率, 实际用词表, 基础词项数, 配置词表规模)。
    """
    terms, base_n, vocab_n = build_lexical_terms_for_rerank(question, domain_cfg)
    if not terms or not hits:
        return hits[: int(final_top_k)], 0.0, terms, base_n, vocab_n

    scored: list[tuple[str, float, float, float]] = []
    hit_rates: list[float] = []

    blob_cache: dict[str, str] = {}

    for mid, vec_s in hits:
        node = get_node(mid)
        if mid not in blob_cache:
            blob_cache[mid] = method_text_blob_for_rerank(
                mid,
                node,
                method_interpret_store=method_interpret_store,
                business_interpret_store=business_interpret_store,
            )
        blob = blob_cache[mid]
        flat_blob = soft_token_flat_for_ascii_match(blob)
        nd = node or {}
        name_only = str(nd.get("name") or "")
        sig_only = str(nd.get("signature") or "")
        ident_blob = "\n".join(
            [
                name_only,
                sig_only,
                split_identifier_for_lexical_match(name_only),
                split_identifier_for_lexical_match(sig_only),
            ]
        )
        flat_ident = soft_token_flat_for_ascii_match(ident_blob)
        hits_n = 0
        ident_hits = 0
        for t in terms:
            if t.isascii():
                in_blob = ascii_lexical_hit(t, flat_blob)
                if in_blob:
                    hits_n += 1
                if ascii_lexical_hit(t, flat_ident):
                    ident_hits += 1
            else:
                in_blob = t in blob
                if in_blob:
                    hits_n += 1
                if t in name_only or t in sig_only:
                    ident_hits += 1
        ratio = hits_n / max(len(terms), 1)
        ident_ratio = ident_hits / max(len(terms), 1)
        hit_rates.append(ratio)
        final_s = (
            float(vec_s)
            + RERANK_LEXICAL_WEIGHT * ratio
            + RERANK_IDENT_LEXICAL_WEIGHT * ident_ratio
        )
        scored.append((mid, float(vec_s), ratio, final_s))

    scored.sort(key=lambda x: x[3], reverse=True)
    out = [(x[0], x[3]) for x in scored[: int(final_top_k)]]
    avg_lex = sum(hit_rates) / max(len(hit_rates), 1)
    return out, avg_lex, terms, base_n, vocab_n
