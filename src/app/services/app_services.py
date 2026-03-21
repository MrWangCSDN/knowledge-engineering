from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from src.app.services.interpretation_progress import InterpretationProgressService
from src.app.services.pipeline_runner import PipelineRunner
from src.app.services.weaviate_data_service import WeaviateDataService
from src.persistence.repositories import FileStructureFactsRepository, GraphSnapshotRepository
from src.pipeline.gateways import get_interpretation_progress
from src.service.api import get_graph_optional, get_neo4j_backend_optional, set_global_graph


class AppServices:
    """应用装配容器（Composition Root）."""

    def __init__(self, *, root: Path, load_config_fn: Callable[[str | Path], Any]):
        self.root = root
        self.load_config_fn = load_config_fn

        self._pipeline_live: dict[str, Any] = {}
        self._interp_progress_svc: InterpretationProgressService | None = None
        self._weaviate_data_svc: WeaviateDataService | None = None
        self._structure_facts_repo: FileStructureFactsRepository | None = None
        self._snapshot_repo: GraphSnapshotRepository | None = None
        self._pipeline_runner: PipelineRunner | None = None

    @property
    def pipeline_live(self) -> dict[str, Any]:
        if not self._pipeline_live:
            self._pipeline_live.update(
                {
                    "running": False,
                    "completed": False,
                    "mode": "",  # "full" | "interpret_only"
                    "status": "",
                    "progress_md": "",
                    "progress_frac": 0.0,
                    "progress_label": "",
                    "stats_md": "",
                    "checklist_md": "",
                    "steps": [],
                    "checklist_tech": [],
                    "checklist_biz": [],
                    "interpret_current_tech": None,
                    "interpret_current_biz": None,
                    "interp_stats": {},
                    "sf_path": "",
                    "result": None,
                }
            )
        return self._pipeline_live

    @property
    def interp_progress_svc(self) -> InterpretationProgressService:
        if self._interp_progress_svc is None:
            self._interp_progress_svc = InterpretationProgressService(
                root=self.root,
                get_weaviate_progress=get_interpretation_progress,
            )
        return self._interp_progress_svc

    @property
    def structure_facts_repo(self) -> FileStructureFactsRepository:
        if self._structure_facts_repo is None:
            self._structure_facts_repo = FileStructureFactsRepository()
        return self._structure_facts_repo

    @property
    def snapshot_repo(self) -> GraphSnapshotRepository:
        if self._snapshot_repo is None:
            self._snapshot_repo = GraphSnapshotRepository()
        return self._snapshot_repo

    @property
    def weaviate_data_svc(self) -> WeaviateDataService:
        if self._weaviate_data_svc is None:
            self._weaviate_data_svc = WeaviateDataService(config_loader=self.load_config_fn, root=self.root)
        return self._weaviate_data_svc

    @property
    def pipeline_runner(self) -> PipelineRunner:
        if self._pipeline_runner is None:
            self._pipeline_runner = PipelineRunner(
                interp_progress_repo=self.interp_progress_svc,
                structure_facts_repo=self.structure_facts_repo,
                snapshot_repo=self.snapshot_repo,
                get_pipeline_live=lambda: self.pipeline_live,
                get_graph_optional=get_graph_optional,
                root=self.root,
            )
        return self._pipeline_runner

    @staticmethod
    def get_graph_optional() -> Any:
        return get_graph_optional()

    @staticmethod
    def set_global_graph(graph: Any) -> None:
        set_global_graph(graph)

    @staticmethod
    def get_neo4j_backend_optional() -> Any:
        return get_neo4j_backend_optional()
