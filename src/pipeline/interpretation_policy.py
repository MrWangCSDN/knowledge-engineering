"""解读相关流水线策略：集中计算「是否跑拓扑解读」等布尔组合，避免散落重复判断。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.config.models import KnowledgeConfig


@dataclass(frozen=True)
class InterpretationPipelinePolicy:
    """由 ``KnowledgeConfig`` + 用户覆盖项推导出的解读阶段策略。

    - **interpret_enabled**：配置层拓扑解读与 vectordb-interpret 均开启（不检查 backend，供结构层进度条比例等）。
    - **run_interpret_phase**：完整流水线中是否进入解读 Stage。
    - **tech_batch_runnable**：「仅解读」入口在用户勾选时的可执行条件（含 weaviate）。
    """

    mi_on: bool
    vinterp_on: bool
    vinterp_backend: str
    interpret_enabled: bool
    want_interpret: bool
    run_interpret_phase: bool

    @classmethod
    def from_knowledge_config(
        cls,
        k: KnowledgeConfig,
        *,
        include_method_interpretation: Optional[bool] = None,
    ) -> InterpretationPipelinePolicy:
        pipe = k.pipeline
        mi_on = k.topological_interpretation.enabled
        vinterp_on = k.vectordb_interpret.enabled
        vinterp_backend = (k.vectordb_interpret.backend or "").strip().lower()
        interpret_enabled = mi_on and vinterp_on
        want_interpret = (
            include_method_interpretation
            if include_method_interpretation is not None
            else pipe.include_topological_interpretation_build
        )
        run_interpret_phase = want_interpret and interpret_enabled

        return cls(
            mi_on=mi_on,
            vinterp_on=vinterp_on,
            vinterp_backend=vinterp_backend,
            interpret_enabled=interpret_enabled,
            want_interpret=want_interpret,
            run_interpret_phase=run_interpret_phase,
        )

    def tech_batch_runnable(self, user_include: bool) -> bool:
        """仅解读：用户勾选且 topological_interpretation + vectordb-interpret 为 weaviate。"""
        return user_include and self.mi_on and self.vinterp_on and self.vinterp_backend == "weaviate"
