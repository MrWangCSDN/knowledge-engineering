"""
sse_emitter done 事件透传 cited_entities（设计 §8）。
重依赖异步生成器，沿用源码不变量手法：done 事件 dict 含 cited_entities: answer.cited_entities。
收集逻辑由 test_qa_react_synthesizer 真单测覆盖。
"""
from pathlib import Path


def test_done_event_includes_cited_entities():
    src = Path("src/service/qa_engine/sse_emitter.py").read_text(encoding="utf-8")
    # done 事件块里透传 answer.cited_entities
    assert "answer.cited_entities" in src
    assert '"cited_entities"' in src
