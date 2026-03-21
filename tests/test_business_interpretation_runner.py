"""业务解读 Runner：跳过分支与策略对象构造（避免与 Weaviate/LLM 强耦合）。"""
from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from src.config.models import BusinessInterpretationConfig, VectorDBConfig
from src.knowledge.business_interpretation_runner import run_business_interpretations
from src.knowledge.business_interpretation_strategies import BusinessInterpretTierSpec
from src.models import DomainKnowledge
from src.models.structure import StructureFacts


def _empty_facts() -> StructureFacts:
    return StructureFacts(entities=[], relations=[])


def _empty_domain() -> DomainKnowledge:
    return DomainKnowledge()


def test_run_skipped_when_business_interpretation_disabled() -> None:
    biz = BusinessInterpretationConfig(enabled=False)
    vdb = VectorDBConfig(enabled=True, backend="weaviate")
    out = run_business_interpretations(_empty_facts(), _empty_domain(), biz, vdb)
    assert out["skipped"] is True
    assert out["written"] == 0
    assert out["failed"] == 0


def test_run_skipped_when_vectordb_backend_not_weaviate() -> None:
    biz = BusinessInterpretationConfig(enabled=True)
    vdb = VectorDBConfig(enabled=True, backend="chromadb")
    out = run_business_interpretations(_empty_facts(), _empty_domain(), biz, vdb)
    assert out["skipped"] is True
    assert out["written"] == 0


def test_run_skipped_when_vectordb_disabled() -> None:
    biz = BusinessInterpretationConfig(enabled=True)
    vdb = VectorDBConfig(enabled=False, backend="weaviate")
    out = run_business_interpretations(_empty_facts(), _empty_domain(), biz, vdb)
    assert out["skipped"] is True


def test_business_interpret_tier_spec_is_frozen() -> None:
    tier = BusinessInterpretTierSpec(
        items=(),
        msg_prefix="x",
        min_text_len=1,
        pct_cap=1,
        label_fn=lambda x: str(x),
        prompt_fn=lambda x: "",
        add_kwargs_fn=lambda x, t: {},
    )
    with pytest.raises(FrozenInstanceError):
        tier.msg_prefix = "y"  # type: ignore[misc]
