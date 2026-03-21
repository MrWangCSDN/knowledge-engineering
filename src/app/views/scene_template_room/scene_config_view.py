"""样板间配置防腐层：只读视图，避免 UI 直接解析 ``repo_cfg['knowledge']['vectordb-…']`` 等魔法路径。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.config.models import KnowledgeConfig, ProjectConfig


@dataclass(frozen=True)
class SceneTemplateConfigView:
    """由 ``ProjectConfig`` 派生的窄接口，供场景与向量重排等使用。"""

    knowledge: KnowledgeConfig
    domain: dict[str, Any]

    @classmethod
    def empty(cls) -> SceneTemplateConfigView:
        return cls(knowledge=KnowledgeConfig(), domain={})

    @classmethod
    def from_project_config(cls, cfg: ProjectConfig | None) -> SceneTemplateConfigView:
        if cfg is None:
            return cls.empty()
        dom = cfg.domain if isinstance(cfg.domain, dict) else {}
        return cls(knowledge=cfg.knowledge, domain=dict(dom))

    @classmethod
    def from_yaml_dict(cls, raw: dict[str, Any] | None) -> SceneTemplateConfigView:
        if not raw:
            return cls.empty()
        return cls.from_project_config(ProjectConfig.from_yaml_dict(raw))

    @property
    def yaml_graph_backend(self) -> str:
        """``knowledge.graph.backend``（project.yaml 声明）。"""
        return (self.knowledge.graph.backend or "").strip() or "memory"
