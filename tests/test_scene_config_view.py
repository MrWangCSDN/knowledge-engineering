from src.app.views.scene_template_room.scene_config_view import SceneTemplateConfigView
from src.knowledge.business_question_lexical_rerank import (
    build_lexical_terms_for_rerank,
    collect_domain_lexical_vocab,
)
from src.config.models import ProjectConfig


def test_scene_template_config_view_from_yaml_dict():
    raw = {
        "domain": {
            "business_domains": [{"id": "d1", "name": "订单域"}],
            "capabilities": [{"id": "c1", "name": "商品管理"}],
            "terms": [{"name": "SKU", "synonyms": ["sku"]}],
        },
        "knowledge": {
            "graph": {"backend": "neo4j"},
            "vectordb-interpret": {"collection_name": "InterpCol", "dimension": 512},
        },
        "schema": {"ddl_path": "x.sql", "mapper_glob": "*.xml"},
    }
    v = SceneTemplateConfigView.from_yaml_dict(raw)
    assert v.yaml_graph_backend == "neo4j"
    assert "订单域" in collect_domain_lexical_vocab(v.domain)
    terms, _, _ = build_lexical_terms_for_rerank("订单与商品", v.domain)
    assert len(terms) >= 1

    pc = ProjectConfig.from_yaml_dict(raw)
    dumped = pc.model_dump()
    assert dumped.get("schema", {}).get("ddl_path") == "x.sql"
    assert pc.table_access_schema.get("ddl_path") == "x.sql"


def test_empty_config_view():
    v = SceneTemplateConfigView.empty()
    assert v.yaml_graph_backend == "memory"
    assert collect_domain_lexical_vocab(v.domain) == frozenset()
