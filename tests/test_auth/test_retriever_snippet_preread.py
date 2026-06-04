"""retriever 源码预读（source-first grounding P1）：top-3、整方法全文、超大截断、None 跳过、带出。

P1 设计见 Obsidian [[业务问答-源码优先接地-P1设计]]。本文件测：
- `_truncate_snippet` 纯函数：常规方法全文返回、病态超大方法截断 + 标注
- `QARetriever._enrich_candidate_snippets`：仅 top-3、跳过取不到的候选、store 无该方法时不崩
- `_ctx_to_dict`：把 candidate_code_snippets 带进 build_user_prompt 用的 dict
"""
# 被测：模块级纯函数 + 数据类 + 检索器
from src.service.qa_engine.retriever import _truncate_snippet, RetrievedContext, QARetriever
# 被测：ctx → dict 的转换（build_user_prompt 的入参来源）
from src.service.qa_engine.synthesizer import _ctx_to_dict


def test_truncate_keeps_short_method_full():
    """常规方法（行数/字符都在上限内）→ 原样全文返回，不截断。"""
    s = "line1\nline2\nline3"
    assert _truncate_snippet(s) == s


def test_truncate_marks_oversized_method():
    """病态超大方法（> 300 行）→ 截到上限 + 末尾标注原始行数，提示可 ke_read_entity 取全文。"""
    # 生成 400 行源码（超过 300 行上限）
    s = "\n".join(f"l{i}" for i in range(400))
    out = _truncate_snippet(s)
    assert "已截断" in out and "400 行" in out      # 标注出现 + 原始行数可见
    assert out.count("\n") <= 305                  # 截到上限附近（300 行 join + 1 标注行）


def test_enrich_only_top3_and_skips_none():
    """只预读 top-3 候选；某候选取不到（None）时跳过、不影响其余、不报错。"""
    # getter 对 B 返回 None（应跳过）、A/C/D 返回源码；但 D 是第 4 个候选、不在 top-3
    snippets = {"A::a#()": "code A", "B::b#()": None, "C::c#()": "code C", "D::d#()": "code D"}
    class _Comp:
        def get_code_snippet(self, eid):
            return snippets.get(eid)
    class _Graph:
        def module_of(self, eid):
            return None
    # QARetriever.__init__ 是 keyword-only（签名含 `*`）
    r = QARetriever(interpretation_store=_Comp(), graph=_Graph(), recall_threshold=0.45)
    ctx = RetrievedContext(question="q", project_id="p")
    ctx.entry_candidates = [{"entity_id": k} for k in ["A::a#()", "B::b#()", "C::c#()", "D::d#()"]]
    r._enrich_candidate_snippets(ctx)
    # top-3 = A,B,C；B 取到 None 被跳过；D 第4个不预读 → 仅 {A, C}
    assert set(ctx.candidate_code_snippets) == {"A::a#()", "C::c#()"}
    assert ctx.candidate_code_snippets["A::a#()"] == "code A"


def test_enrich_noop_when_store_lacks_method():
    """interpretation_store（旧实例）无 get_code_snippet → 整体跳过，不崩、留空 dict。"""
    class _Comp:        # 无 get_code_snippet
        pass
    class _Graph:
        def module_of(self, eid):
            return None
    r = QARetriever(interpretation_store=_Comp(), graph=_Graph(), recall_threshold=0.45)
    ctx = RetrievedContext(question="q", project_id="p")
    ctx.entry_candidates = [{"entity_id": "A::a#()"}]
    r._enrich_candidate_snippets(ctx)               # 不抛
    assert ctx.candidate_code_snippets == {}


def test_ctx_to_dict_carries_snippets():
    """_ctx_to_dict 必须把 candidate_code_snippets 带出，供 build_user_prompt 渲染。"""
    ctx = RetrievedContext(question="q", project_id="p")
    ctx.candidate_code_snippets = {"A::a#()": "code A"}
    assert _ctx_to_dict(ctx)["candidate_code_snippets"] == {"A::a#()": "code A"}
