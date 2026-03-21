"""解读 Runner 入参：优先使用 ``src.config.models`` 中的 Pydantic 对象，仍兼容 dict。"""
from __future__ import annotations

from typing import Any, Mapping, Union

from src.config.models import (
    BusinessInterpretationConfig,
    MethodInterpretationConfig,
    VectorDBConfig,
)

MethodInterpretInput = Union[MethodInterpretationConfig, Mapping[str, Any]]
BusinessInterpretInput = Union[BusinessInterpretationConfig, Mapping[str, Any]]
VectorDbInterpretInput = Union[VectorDBConfig, Mapping[str, Any]]


def coerce_method_interpretation_config(cfg: MethodInterpretInput) -> MethodInterpretationConfig:
    if isinstance(cfg, MethodInterpretationConfig):
        return cfg
    return MethodInterpretationConfig.model_validate(dict(cfg))


def coerce_business_interpretation_config(cfg: BusinessInterpretInput) -> BusinessInterpretationConfig:
    if isinstance(cfg, BusinessInterpretationConfig):
        return cfg
    return BusinessInterpretationConfig.model_validate(dict(cfg))


def coerce_vectordb_config(cfg: VectorDbInterpretInput) -> VectorDBConfig:
    if isinstance(cfg, VectorDBConfig):
        return cfg
    return VectorDBConfig.model_validate(dict(cfg))
