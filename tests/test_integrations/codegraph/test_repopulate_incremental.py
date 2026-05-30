"""增量更新单测：内容 hash 变才重 embed；孤儿 key 删除。"""
from src.integrations.codegraph.content_checkpoint import ContentCheckpoint


def test_content_checkpoint_detects_change(tmp_path):
    cp = ContentCheckpoint(path=str(tmp_path / "cc.json"))
    assert cp.changed("A::m#()", "code-v1") is True       # 新 key → 算变
    cp.mark("A::m#()", "code-v1")
    assert cp.changed("A::m#()", "code-v1") is False      # 内容没变 → 不重 embed
    assert cp.changed("A::m#()", "code-v2") is True       # 内容变了 → 重 embed


def test_content_checkpoint_orphans(tmp_path):
    cp = ContentCheckpoint(path=str(tmp_path / "cc.json"))
    cp.mark("A::a#()", "x"); cp.mark("A::b#()", "y")
    assert cp.orphans({"A::a#()"}) == {"A::b#()"}
