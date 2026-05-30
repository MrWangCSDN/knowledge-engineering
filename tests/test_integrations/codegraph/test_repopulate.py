# tests/test_integrations/codegraph/test_repopulate.py
"""重灌编排单测：节点→(key,name,snippet)→embed→store；用假依赖。"""
from tests.test_integrations.codegraph._fixture import make_fixture_db
from src.integrations.codegraph.db import CodeGraphDB
from src.integrations.codegraph.repopulate import repopulate_code_entities


class _FakeEmbedder:
    """假 embedder：每条文本返回固定假向量，记录被 embed 的文本。"""
    def __init__(self):
        self.embedded = []
    def __call__(self, texts):
        self.embedded.extend(texts)
        return [[0.1, 0.2, 0.3] for _ in texts]


class _FakeStore:
    """假向量库：记录 add 调用。"""
    def __init__(self):
        self.added = []
    def add(self, entity_id, vector, entity_type=None, name=None, code_snippet=None, *, tenant=None):
        self.added.append({"id": entity_id, "name": name, "snippet": code_snippet, "tenant": tenant})


def test_repopulate_keys_by_durable_key(tmp_path):
    db_path = str(tmp_path / "codegraph.db")
    make_fixture_db(db_path)
    # 给 repo_root 造出节点 file_path 对应的文件，让片段非空（夹具行号 ≤ 50）
    for fn in ("Ctrl.java", "Svc.java", "Dao.java"):
        (tmp_path / fn).write_text("x\n" * 50, encoding="utf-8")

    embedder, store = _FakeEmbedder(), _FakeStore()
    stats = repopulate_code_entities(
        db=CodeGraphDB(db_path),
        repo_root=str(tmp_path),
        project_id="mall-swarm",
        embed_batch=embedder,
        store=store,
    )
    ids = sorted(a["id"] for a in store.added)
    assert "OmsService::generateOrder#(OrderParam)" in ids
    assert "OmsOrderDao::save#(OmsOrder)" in ids
    assert all(a["tenant"] == "mall-swarm" for a in store.added)
    assert stats["embedded"] == 3
