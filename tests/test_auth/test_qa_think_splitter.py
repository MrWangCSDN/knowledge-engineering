# tests/test_auth/test_qa_think_splitter.py
"""
ThinkSplitter：把逐 chunk 文本流切成 think/text 段。
处理 <think>...</think> 跨 chunk 被切碎的标签（'<thi'+'nk>' 类）。
MiniMax 流式（含工具）路径用它把推理段路由到 StreamThinkingDelta。
"""
from src.service.qa_engine.think_splitter import ThinkSplitter, Segment


def _drain(splitter: ThinkSplitter, chunks: list[str]) -> list[Segment]:
    """喂入所有 chunk + flush，收集全部 Segment。"""
    out: list[Segment] = []
    for c in chunks:
        out.extend(splitter.feed(c))
    out.extend(splitter.flush())
    return out


def test_plain_text_no_think():
    # 没有 think 标签 → 全是 text 段
    segs = _drain(ThinkSplitter(), ["你好", "世界"])
    assert all(s.kind == "text" for s in segs)
    assert "".join(s.text for s in segs) == "你好世界"


def test_single_think_segment_one_chunk():
    # 一个 chunk 内含完整 <think>...</think>
    segs = _drain(ThinkSplitter(), ["答案前<think>推理中</think>答案后"])
    think = "".join(s.text for s in segs if s.kind == "think")
    text = "".join(s.text for s in segs if s.kind == "text")
    assert think == "推理中"
    assert text == "答案前答案后"


def test_open_tag_split_across_chunks():
    # 开标签被切碎：'<thi' + 'nk>推理</think>正文'
    segs = _drain(ThinkSplitter(), ["正文A<thi", "nk>推理X</think>正文B"])
    think = "".join(s.text for s in segs if s.kind == "think")
    text = "".join(s.text for s in segs if s.kind == "text")
    assert think == "推理X"
    assert text == "正文A正文B"


def test_close_tag_split_across_chunks():
    # 闭标签被切碎：'<think>推理</thi' + 'nk>正文'
    segs = _drain(ThinkSplitter(), ["<think>推理Y</thi", "nk>正文C"])
    think = "".join(s.text for s in segs if s.kind == "think")
    text = "".join(s.text for s in segs if s.kind == "text")
    assert think == "推理Y"
    assert text == "正文C"


def test_think_content_spanning_multiple_chunks():
    # think 段内容跨多个 chunk（无标签的中段）
    segs = _drain(ThinkSplitter(), ["<think>推", "理", "Z</think>尾"])
    think = "".join(s.text for s in segs if s.kind == "think")
    text = "".join(s.text for s in segs if s.kind == "text")
    assert think == "推理Z"
    assert text == "尾"


def test_unclosed_think_flushes_as_think():
    # 流结束时仍在 think 段（没等到闭标签）→ 残余作为 think 段 flush
    segs = _drain(ThinkSplitter(), ["正文<think>未闭合推理"])
    think = "".join(s.text for s in segs if s.kind == "think")
    text = "".join(s.text for s in segs if s.kind == "text")
    assert text == "正文"
    assert think == "未闭合推理"


def test_segment_is_frozen():
    import dataclasses
    s = Segment(kind="text", text="x")
    try:
        s.text = "y"  # type: ignore[misc]
        assert False, "应抛 FrozenInstanceError"
    except dataclasses.FrozenInstanceError:
        pass
