from src.knowledge.business_interpretation_context import format_domain_background
from src.models import DomainKnowledge


def test_format_domain_background_empty() -> None:
    assert format_domain_background(DomainKnowledge()) == ""
