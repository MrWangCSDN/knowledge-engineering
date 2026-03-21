"""流水线配置加载与领域模型构造（无 Stage 依赖）。"""
from __future__ import annotations

from pathlib import Path
from typing import Union

from src.config import ProjectConfig
from src.models import DomainKnowledge, BusinessDomain, ServiceDomainMapping


def load_config(config_path: str | Path) -> ProjectConfig:
    """加载 YAML 配置并解析为强类型 ProjectConfig。"""
    import yaml

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return ProjectConfig.from_yaml_dict(raw)


def config_to_domain(config: Union[dict, ProjectConfig]) -> DomainKnowledge:
    """从 config['domain'] 构建 DomainKnowledge。"""
    dom = config.domain if isinstance(config, ProjectConfig) else (config.get("domain") or {})
    domains = [
        BusinessDomain(
            id=d.get("id", ""),
            name=d.get("name"),
            description=d.get("description"),
            capability_ids=d.get("capability_ids") or [],
            term_ids=d.get("term_ids") or [],
        )
        for d in dom.get("business_domains") or []
    ]
    caps = dom.get("capabilities") or []
    terms = dom.get("terms") or []
    mappings = [
        ServiceDomainMapping(
            service_or_module_id=m.get("service_or_module_id", ""),
            business_domain_ids=m.get("business_domain_ids") or [],
            weight=float(m.get("weight", 1.0)),
        )
        for m in dom.get("service_domain_mappings") or []
    ]
    return DomainKnowledge(
        business_domains=domains,
        capabilities=caps,
        terms=terms,
        service_domain_mappings=mappings,
    )
