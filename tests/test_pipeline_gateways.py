"""应用层网关：与 config_bootstrap 行为一致。"""
from __future__ import annotations

import pytest

from src.pipeline.config_bootstrap import load_config
from src.pipeline.gateways import load_project_config


def test_gateway_load_raises_for_missing_file() -> None:
    with pytest.raises(FileNotFoundError):
        load_config("__no_such_config__.yaml")
    with pytest.raises(FileNotFoundError):
        load_project_config("__no_such_config__.yaml")
